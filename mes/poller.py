"""Polls GVL.Reg and the warehouse counters; notifies cost tracker."""
import asyncio
import time

from config import (POLL_REG_INTERVAL, POLL_WAREHOUSE_INTERVAL, REG_SIZE)


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
                    # Reg[i] represents the piece currently inside machine i
                    # (the PDF says GVL.Reg[0] for Cell 1, etc.). We map the
                    # index back to a cell number when possible.
                    cell = (i // 3) + 1  # 3 machines per cell as per layout
                    if prev == 0 and now != 0:
                        self.cost.machine_entered(cell, int(now), time.time())
                    elif prev != 0 and now == 0:
                        self.cost.machine_left(cell, int(prev), time.time())
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