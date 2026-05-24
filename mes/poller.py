"""Polls GVL.Reg and the warehouse counters; notifies cost tracker."""
import asyncio
import time

from config import (POLL_REG_INTERVAL, POLL_WAREHOUSE_INTERVAL, REG_SIZE)


# Map Reg[index] -> cell number.
# This MUST match the Roff values used in PLC_PRG when instantiating
# each Cell:
#   Cell_1: Wout_R=5, Win_R=0
#   Cell_2: Wout_R=6, Win_R=1
#   Cell_3: Wout_R=7, Win_R=2
#   Cell_4: Wout_R=8, Win_R=3
# Any other index is ignored.
REG_TO_CELL = {
    5: 1, 0: 1,   # Cell_1 (Wout=5, Win=0)
    6: 2, 1: 2,   # Cell_2 (Wout=6, Win=1)
    7: 3, 2: 3,   # Cell_3 (Wout=7, Win=2)
    8: 4, 3: 4,   # Cell_4 (Wout=8, Win=3)
}


class Poller:
    def __init__(self, plc, cost_tracker, on_warehouse_update=None):
        self.plc = plc
        self.cost = cost_tracker
        self.on_warehouse_update = on_warehouse_update
        self._last_reg = [0] * REG_SIZE
        self._w1, self._w2 = 0, 0

    async def reg_loop(self):
        while True:
            try:
                cur = await self.plc.read_reg()
                for i, (prev, now) in enumerate(zip(self._last_reg, cur)):
                    if prev == now:
                        continue
                    cell = REG_TO_CELL.get(i)
                    if cell is None:
                        continue
                    if prev == 0 and now != 0:
                        self.cost.machine_entered(cell, int(now),
                                                  time.time())
                    elif prev != 0 and now == 0:
                        self.cost.machine_left(cell, int(prev),
                                               time.time())
                self._last_reg = cur
            except Exception as e:
                print(f"[poll] reg error: {e}")
            await asyncio.sleep(POLL_REG_INTERVAL)

    async def warehouse_loop(self):
        while True:
            try:
                w1 = await self.plc.read_w1_count()
                w2 = await self.plc.read_w2_count()
                if (w1, w2) != (self._w1, self._w2):
                    self._w1, self._w2 = w1, w2
                    if self.on_warehouse_update:
                        self.on_warehouse_update(w1, w2)
            except Exception as e:
                print(f"[poll] warehouse error: {e}")
            await asyncio.sleep(POLL_WAREHOUSE_INTERVAL)