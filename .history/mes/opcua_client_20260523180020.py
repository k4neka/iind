"""OPC-UA client wrapping all PLC reads/writes."""
import asyncio
import datetime
from asyncua import Client, ua

from config import (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK,
                    OPCUA_NS, OPCUA_PREFIX, REG_SIZE, LOADER_BATCH_SIZE)


class PLCClient:
    def __init__(self):
        self.client: Client | None = None
        # Cache do VariantType correto para OpTime — descoberto na 1ª escrita
        self._optime_variant = None  # None | "timedelta" | ua.VariantType

    # ---------- Connection ----------
    async def connect(self) -> bool:
        for url in (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK):
            try:
                print(f"[opcua] connecting to {url}...")
                c = Client(url=url)
                await c.connect()
                self.client = c
                print(f"[opcua] connected to {url}")
                return True
            except Exception as e:
                print(f"[opcua] failed {url}: {e}")
        return False

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    # ---------- Node helpers ----------
    def _node(self, var: str):
        return self.client.get_node(f"ns={OPCUA_NS};s={OPCUA_PREFIX}{var}")

    async def read(self, var: str):
        return await self._node(var).read_value()

    async def write(self, var: str, value,
                    varianttype: ua.VariantType | None = None):
        n = self._node(var)
        if varianttype is None:
            await n.write_value(value)
        else:
            await n.write_value(ua.DataValue(ua.Variant(value, varianttype)))

    # ---------- High-level reads ----------
    async def read_reg(self) -> list[int]:
        return [await self.read(f"Reg[{i}]") for i in range(REG_SIZE)]

    async def read_w1_count(self) -> int:
        try:
            return int(await self.read("g_W1_Count"))
        except Exception as e:
            print(f"[opcua] read_w1_count failed: {e}")
            return 0

    async def read_w2_count(self) -> int:
        try:
            return int(await self.read("g_W2_Count"))
        except Exception as e:
            print(f"[opcua] read_w2_count failed: {e}")
            return 0

    async def cell_free(self, cell: int) -> bool:
        return bool(await self.read(f"Cell_{cell}_Top_Status.free_cmd"))

    # ---------- Writes: dispatch a workpiece to a cell ----------
    async def write_workpiece(self, cell: int, init_piece: int,
                              operations: list[dict]):
        """
        Constrói Workpiece_T em Cell_X_Top_Order.Workpiece e levanta recv_cmd.
        Se algo falhar (BadTypeMismatch, etc.) PROPAGA a exceção para o
        dispatcher poder reagir (não marcar como dispatched).
        """
        base = f"Cell_{cell}_Top_Order.Workpiece"
        await self.write(f"{base}.InitPiece", int(init_piece),
                         ua.VariantType.Int16)
        await self.write(f"{base}.Next_Operation", 0, ua.VariantType.Int16)
        await self.write(f"{base}.Last_Operation",
                         max(0, len(operations) - 1), ua.VariantType.Int16)

        for i, op in enumerate(operations[:11]):
            op_base = f"{base}.Operations[{i}]"
            await self.write(f"{op_base}.Cell",    int(op["cell"]),
                             ua.VariantType.Int16)
            await self.write(f"{op_base}.Machine", int(op["machine"]),
                             ua.VariantType.Int16)
            await self.write(f"{op_base}.Tool",    int(op["tool"]),
                             ua.VariantType.Int16)

            await self._write_optime(f"{op_base}.OpTime", op["op_time_s"])

        # Sinal final ao PLC
        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         True, ua.VariantType.Boolean)

    async def _write_optime(self, node_path: str, op_time_s: float):
        """Sonda + cache do VariantType correto para CODESYS TIME."""
        node = self._node(node_path)
        ms = int(op_time_s * 1000)

        # Se já sabemos qual funciona, usar direto
        if self._optime_variant is not None:
            return await self._write_optime_with(node, ms, self._optime_variant)

        # Sonda: tenta vários tipos pela ordem mais provável
        candidates = [
            ua.VariantType.Int64,
            ua.VariantType.Int32,
            ua.VariantType.UInt64,
            ua.VariantType.UInt32,
            ua.VariantType.Double,
            ua.VariantType.Float,
            "timedelta",
        ]

        # Tenta primeiro descobrir o tipo via read
        try:
            current = await node.read_value()
            current_type = type(current).__name__
            if isinstance(current, datetime.timedelta):
                # Tenta timedelta primeiro
                candidates = ["timedelta"] + [c for c in candidates if c != "timedelta"]
            elif isinstance(current, float):
                candidates = [ua.VariantType.Double, ua.VariantType.Float] + \
                             [c for c in candidates
                              if c not in (ua.VariantType.Double, ua.VariantType.Float)]
            print(f"[opcua] OpTime probe: node returned {current!r} (type={current_type})")
        except Exception:
            pass

        last_err = None
        for vt in candidates:
            try:
                await self._write_optime_with(node, ms, vt)
                self._optime_variant = vt
                name = vt if isinstance(vt, str) else vt.name
                print(f"[opcua] OpTime VariantType detected: {name}")
                return
            except Exception as e:
                last_err = e
                continue

        print(f"[opcua] OpTime: ALL write attempts failed. Last error: {last_err}")
        raise last_err

    async def _write_optime_with(self, node, ms: int, vt):
        if vt == "timedelta":
            await node.write_value(datetime.timedelta(milliseconds=ms))
        elif vt in (ua.VariantType.Double, ua.VariantType.Float):
            await node.write_value(ua.DataValue(ua.Variant(float(ms), vt)))
        else:
            await node.write_value(ua.DataValue(ua.Variant(int(ms), vt)))

    # ---------- Loader ----------
    async def trigger_loader_batch(self, wood_qty: int, metal_qty: int):
        """Dispara o loader com um SINGLE batch (já dimensionado p/ <= 5 peças).
        Não faz throttling — quem chama (flush_loader) trata dos lotes."""
        if wood_qty == 0 and metal_qty == 0:
            return
        try:
            # 1) Baixar Exec
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)

            # 2) Esperar IDLE
            idle = False
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    idle = True
                    break
                await asyncio.sleep(0.1)
            if not idle:
                print("[opcua] trigger_loader_batch: PLC nunca chegou a IDLE em 5s")
                return

            # 3) Targets
            await self.write("g_Loader_Wood_Qty",  int(wood_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Metal_Qty", int(metal_qty),
                             ua.VariantType.Int16)

            # 4) Exec rising edge
            await self.write("g_Loader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_loader_batch wood={wood_qty} metal={metal_qty}")
        except Exception as e:
            print(f"[opcua] trigger_loader_batch failed: {e}")

    async def trigger_loader(self, wood_qty: int, metal_qty: int):
        """Compatibilidade — dispara um único batch sem throttling.
        Para throttling em lotes usa-se trigger_loader_batched()."""
        await self.trigger_loader_batch(wood_qty, metal_qty)

    async def trigger_loader_batched(self, wood_qty: int, metal_qty: int):
        """Divide um pedido grande em lotes de LOADER_BATCH_SIZE peças.
        Espera cada lote chegar a DONE antes de mandar o seguinte.
        Wood é entregue primeiro, depois Metal."""
        remaining_w = max(0, wood_qty)
        remaining_m = max(0, metal_qty)
        batch_num = 0

        while remaining_w > 0 or remaining_m > 0:
            batch_num += 1
            # Construir um lote ≤ LOADER_BATCH_SIZE
            take_w = min(remaining_w, LOADER_BATCH_SIZE)
            take_m = min(remaining_m, LOADER_BATCH_SIZE - take_w)
            print(f"[opcua] batched-load: batch #{batch_num} "
                  f"wood={take_w} metal={take_m} "
                  f"(remaining wood={remaining_w - take_w}, "
                  f"metal={remaining_m - take_m})")

            await self.trigger_loader_batch(take_w, take_m)
            remaining_w -= take_w
            remaining_m -= take_m

            # Esperar DONE (até 120 s por lote)
            done = False
            for _ in range(1200):
                status = await self.read_loader_status()
                if status == 2:   # DONE
                    done = True
                    break
                if status == 3:   # ERROR
                    print(f"[opcua] batched-load: PLC reportou ERROR no batch #{batch_num}")
                    return
                await asyncio.sleep(0.1)
            if not done:
                print(f"[opcua] batched-load: batch #{batch_num} timeout (não chegou a DONE em 120s)")
                return

            # Limpa Exec para próximo rising edge
            await self.clear_loader_exec()
            # Espera o PLC voltar a IDLE antes do próximo batch
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    break
                await asyncio.sleep(0.1)

        print(f"[opcua] batched-load: complete (total wood={wood_qty} metal={metal_qty})")

    async def read_loader_status(self) -> int:
        try:
            return int(await self.read("g_Loader_Status"))
        except Exception as e:
            print(f"[opcua] read_loader_status failed: {e}")
            return 0

    async def clear_loader_exec(self):
        try:
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)
        except Exception as e:
            print(f"[opcua] clear_loader_exec failed: {e}")

    # ---------- Unloader / Return ----------
    async def trigger_unloader(self, piece_type: str, qty: int):
        try:
            await self.write("g_Unloader_PieceType", str(piece_type),
                             ua.VariantType.String)
            await self.write("g_Unloader_Qty", int(qty),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_unloader type={piece_type} qty={qty}")
        except Exception as e:
            print(f"[opcua] trigger_unloader failed: {e}")

    async def trigger_return(self, piece_id: int):
        try:
            await self.write("g_Return_PieceID", int(piece_id),
                             ua.VariantType.Int16)
            await self.write("g_Return_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_return piece_id={piece_id}")
        except Exception as e:
            print(f"[opcua] trigger_return failed: {e}")