"""OPC-UA client wrapping all PLC reads and writes."""
import asyncio
import datetime
from asyncua import Client, ua

from config import (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK,
                    OPCUA_NS, OPCUA_PREFIX, REG_SIZE, LOADER_BATCH_SIZE)


class PLCClient:
    def __init__(self):
        self.client: Client | None = None
        # Cached working VariantType for the CODESYS TIME field OpTime.
        self._optime_variant = None

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

    # ---- low-level helpers --------------------------------------------------
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

    # ---- high-level reads ---------------------------------------------------
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

    # ---- cell handshake -----------------------------------------------------
    async def write_workpiece(self, cell: int, init_piece: int,
                              operations: list[dict]):
        """Populate Cell_X_Top_Order.Workpiece and raise recv_cmd.

        Caller is responsible for closing the handshake (lowering recv_cmd
        once the cell drops free_cmd, then waiting for it to rise again).
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

        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         True, ua.VariantType.Boolean)

    async def clear_recv_cmd(self, cell: int):
        # Lower recv_cmd after the cell accepted the workpiece, matching
        # the Order_generator handshake pattern.
        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         False, ua.VariantType.Boolean)

    # ---- OpTime VariantType probing ----------------------------------------
    async def _write_optime(self, node_path: str, op_time_s: float):
        node = self._node(node_path)
        ms = int(op_time_s * 1000)

        if self._optime_variant is not None:
            return await self._write_optime_with(node, ms,
                                                 self._optime_variant)

        candidates = [
            ua.VariantType.Int64,
            ua.VariantType.Int32,
            ua.VariantType.UInt64,
            ua.VariantType.UInt32,
            ua.VariantType.Double,
            ua.VariantType.Float,
            "timedelta",
        ]
        try:
            current = await node.read_value()
            if isinstance(current, datetime.timedelta):
                candidates = ["timedelta"] + [c for c in candidates
                                              if c != "timedelta"]
            elif isinstance(current, float):
                candidates = [ua.VariantType.Double, ua.VariantType.Float] + \
                             [c for c in candidates
                              if c not in (ua.VariantType.Double,
                                           ua.VariantType.Float)]
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
        print(f"[opcua] OpTime: all write attempts failed: {last_err}")
        raise last_err

    async def _write_optime_with(self, node, ms: int, vt):
        if vt == "timedelta":
            await node.write_value(datetime.timedelta(milliseconds=ms))
        elif vt in (ua.VariantType.Double, ua.VariantType.Float):
            await node.write_value(ua.DataValue(ua.Variant(float(ms), vt)))
        else:
            await node.write_value(ua.DataValue(ua.Variant(int(ms), vt)))

    # ---- Loader -------------------------------------------------------------
    async def trigger_loader_batch(self, wood_qty: int, metal_qty: int):
        """Fire one batch (<= LOADER_BATCH_SIZE pieces) onto the Loader."""
        if wood_qty == 0 and metal_qty == 0:
            return
        try:
            # Clear Exec first so the rising edge is unambiguous.
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)

            # Wait for IDLE before writing new targets.
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                print("[opcua] trigger_loader_batch: PLC never reached IDLE")
                return

            await self.write("g_Loader_Wood_Qty",  int(wood_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Metal_Qty", int(metal_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_loader_batch wood={wood_qty} "
                  f"metal={metal_qty}")
        except Exception as e:
            print(f"[opcua] trigger_loader_batch failed: {e}")

    async def trigger_loader(self, wood_qty: int, metal_qty: int):
        # Backwards-compat alias: single shot, no batching.
        await self.trigger_loader_batch(wood_qty, metal_qty)

    async def trigger_loader_batched(self, wood_qty: int, metal_qty: int):
        """Split a large request into batches of LOADER_BATCH_SIZE pieces.
        Wood is delivered first, then Metal. Waits for DONE between batches.
        """
        remaining_w = max(0, wood_qty)
        remaining_m = max(0, metal_qty)
        batch_num = 0

        while remaining_w > 0 or remaining_m > 0:
            batch_num += 1
            take_w = min(remaining_w, LOADER_BATCH_SIZE)
            take_m = min(remaining_m, LOADER_BATCH_SIZE - take_w)
            print(f"[opcua] batched-load: batch #{batch_num} "
                  f"wood={take_w} metal={take_m} "
                  f"(remaining wood={remaining_w - take_w}, "
                  f"metal={remaining_m - take_m})")

            await self.trigger_loader_batch(take_w, take_m)
            remaining_w -= take_w
            remaining_m -= take_m

            # Wait DONE (up to 120 s per batch).
            done = False
            for _ in range(1200):
                status = await self.read_loader_status()
                if status == 2:
                    done = True
                    break
                if status == 3:
                    print(f"[opcua] batched-load: ERROR on batch "
                          f"#{batch_num}")
                    return
                await asyncio.sleep(0.1)
            if not done:
                print(f"[opcua] batched-load: batch #{batch_num} timeout")
                return

            # Clear Exec so next batch produces a rising edge.
            await self.clear_loader_exec()
            for _ in range(50):
                if (await self.read_loader_status()) == 0:
                    break
                await asyncio.sleep(0.1)

        print(f"[opcua] batched-load: complete "
              f"(total wood={wood_qty} metal={metal_qty})")

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

    # ---- Unloader / Return --------------------------------------------------
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