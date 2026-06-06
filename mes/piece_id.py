"""Single shared wire-PieceID generator for the PLC completion buffer.

PLC `Workpiece_T.PieceID`/`ParentID` are INT16 (-32768..32767) and 0 marks
an empty `g_Done` slot. The PostgreSQL `pending_pieces.id` SERIAL grows
unboundedly and would silently overflow INT16 after enough orders,
corrupting `g_Done`. So the *wire* PieceID/ParentID always come from this
generator (1..30000, never 0); the DB id is only a local lookup key and is
mapped back via `pending_pieces.wire_piece_id` in `completion_loop`.
"""
_counter = 0


def next_piece_id() -> int:
    global _counter
    _counter += 1
    if _counter > 30_000:        # stay inside INT16, never 0
        _counter = 1
    return _counter
