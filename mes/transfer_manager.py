"""TransferManager: single worker that drains pieces from W2 back to W1
through the CODESYS Transfer_Cell.

There is exactly one physical Transfer_Cell, so transfers must be
serialised — but they must also happen *immediately and back-to-back*,
never one-per-simulation-day. This manager keeps a queue and a single
run loop that pulls the next piece the instant the previous transfer
finishes, so a burst of finished sub-parts in W2 is drained in one
continuous sequence.

Used for two things:
  * staging complex-piece sub-parts (RtopW, LegM) from W2 to W1, and
  * returning finished products to W1 on completion.
"""
import asyncio

from transformations import PIECE_ID, ID_TO_PIECE


class TransferManager:
    def __init__(self, plc):
        self.plc = plc
        self._queue: asyncio.Queue = asyncio.Queue()

    @staticmethod
    def _resolve_id(piece):
        if isinstance(piece, str):
            return PIECE_ID.get(piece)
        return int(piece)

    def enqueue(self, piece, on_done=None, label=None):
        """Queue a piece (type name or numeric id) for transfer W2 -> W1.

        `on_done(ok: bool)` is invoked on the event loop after the
        transfer completes. Safe to call from any coroutine on the loop.
        """
        pid = self._resolve_id(piece)
        if pid is None:
            print(f"[transfer] unknown piece '{piece}', skipping")
            if on_done:
                on_done(False)
            return
        name = label or ID_TO_PIECE.get(pid, pid)
        self._queue.put_nowait((pid, on_done, name))
        print(f"[transfer] queued {name} (id={pid}) "
              f"W2->W1 (depth={self._queue.qsize()})")

    async def run_loop(self):
        while True:
            pid, on_done, name = await self._queue.get()
            ok = False
            try:
                # Transfer the instant we dequeue; the next item follows
                # immediately when this returns (sequential, no day wait).
                ok = await self.plc.transfer_piece_blocking(pid)
                if ok:
                    print(f"[transfer] {name} delivered to W1")
                else:
                    print(f"[transfer] {name} FAILED")
            except Exception as e:
                print(f"[transfer] error on {name}: {e}")
            finally:
                if on_done:
                    try:
                        on_done(ok)
                    except Exception as e:
                        print(f"[transfer] on_done error for {name}: {e}")
                self._queue.task_done()
