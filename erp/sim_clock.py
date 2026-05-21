"""Simulation clock: 1 sim day = 60 real seconds. Persistent across restarts."""
import threading
import time

from config import SECONDS_PER_DAY
from database import set_state, get_state


class SimClock:
    def __init__(self):
        self._start_real = time.time()
        self._base_day = int(get_state("sim_base_day", 0) or 0)
        set_state("sim_base_day", self._base_day)
        self._listeners = []
        self._last_day = self.current_day()

    def current_day(self) -> int:
        elapsed = time.time() - self._start_real
        return self._base_day + int(elapsed // SECONDS_PER_DAY)

    def seconds_into_day(self) -> float:
        elapsed = time.time() - self._start_real
        return elapsed % SECONDS_PER_DAY

    def add_day_listener(self, cb):
        self._listeners.append(cb)

    def _run(self):
        while True:
            time.sleep(1)
            d = self.current_day()
            if d != self._last_day:
                self._last_day = d
                set_state("sim_base_day", d)
                for cb in list(self._listeners):
                    try:
                        cb(d)
                    except Exception as e:
                        print(f"[clock] listener error: {e}")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()