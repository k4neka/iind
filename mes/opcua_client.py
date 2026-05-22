"""OPC-UA client wrapping all PLC reads/writes."""
import asyncio
from asyncua import Client, ua

from config import (OPCUA_URL_PRIMARY, OPCUA_URL_FALLBACK,
                    OPCUA_NS, OPCUA_PREFIX, REG_SIZE)


class PLCClient:
    def __init__(self):
        self.client: Client | None = None

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

    async def write(self, var: str, value, varianttype: ua.VariantType | None = None):
        n = self._node(var)
        if varianttype is None:
            await n.write_value(value)
        else:
            await n.write_value(ua.DataValue(ua.Variant(value, varianttype)))

    # ---------- High-level reads ----------
    async def read_reg(self) -> list[int]:
        """Read GVL.Reg[0..14] as a Python list."""
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
        Build Workpiece_T inside Cell_X_Top_Order.Workpiece and set recv_cmd.

        operations = list of dicts with keys: cell, machine, tool, op_time_s
        (max 11 operations because Operations is ARRAY [0..10]).
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
            # OpTime is TIME (ms duration). asyncua maps TIME to UInt32.
            await self.write(f"{op_base}.OpTime",
                             int(op["op_time_s"] * 1000),
                             ua.VariantType.UInt32)

        # Finally raise recv_cmd so the PLC picks up the workpiece.
        await self.write(f"Cell_{cell}_Top_Order.recv_cmd",
                         True, ua.VariantType.Boolean)

    # ---------- Loader ----------

    async def trigger_loader(self, wood_qty: int, metal_qty: int):
        """Write target quantities and pulse g_Loader_Exec.

        The PLC's PRG_Loader detects the rising edge on g_Loader_Exec,
        spawns pieces on the 5 loading lanes and sets g_Loader_Status=2
        (DONE) when finished.  We only set Exec=TRUE here; the poller or
        a dedicated wait loop should clear it once g_Loader_Status==DONE.
        """
        if wood_qty == 0 and metal_qty == 0:
            return
        try:
            await self.write("g_Loader_Wood_Qty",  int(wood_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Metal_Qty", int(metal_qty),
                             ua.VariantType.Int16)
            await self.write("g_Loader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_loader wood={wood_qty} metal={metal_qty}")
        except Exception as e:
            print(f"[opcua] trigger_loader failed: {e}")

    async def read_loader_status(self) -> int:
        """Return g_Loader_Status: 0=IDLE, 1=LOADING, 2=DONE, 3=ERROR."""
        try:
            return int(await self.read("g_Loader_Status"))
        except Exception as e:
            print(f"[opcua] read_loader_status failed: {e}")
            return 0

    async def clear_loader_exec(self):
        """Lower g_Loader_Exec after the PLC reports DONE/ERROR."""
        try:
            await self.write("g_Loader_Exec", False, ua.VariantType.Boolean)
        except Exception as e:
            print(f"[opcua] clear_loader_exec failed: {e}")

    # ---------- Unloader ----------

    async def trigger_unloader(self, piece_type: str, qty: int):
        """Request the PLC unloader to move `qty` pieces of `piece_type`."""
        try:
            await self.write("g_Unloader_PieceType", str(piece_type),
                             ua.VariantType.String)
            await self.write("g_Unloader_Qty", int(qty),
                             ua.VariantType.Int16)
            await self.write("g_Unloader_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_unloader type={piece_type} qty={qty}")
        except Exception as e:
            print(f"[opcua] trigger_unloader failed: {e}")

    # ---------- Return ----------

    async def trigger_return(self, piece_id: int):
        """Request the PLC to return piece_id from W2 to W1 via transfer cell."""
        try:
            await self.write("g_Return_PieceID", int(piece_id),
                             ua.VariantType.Int16)
            await self.write("g_Return_Exec", True, ua.VariantType.Boolean)
            print(f"[opcua] trigger_return piece_id={piece_id}")
        except Exception as e:
            print(f"[opcua] trigger_return failed: {e}")