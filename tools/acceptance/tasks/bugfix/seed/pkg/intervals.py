def merge(intervals):
    """Merge a list of closed integer intervals [a, b] (a <= b). Overlapping or touching intervals
    ([1, 3] and [3, 5], or [1, 3] and [4, 5] since every integer is covered) become one. Returns a new
    sorted list."""
    if not intervals:
        return []
    out = []
    for a, b in sorted(intervals):
        if out and a < out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out
