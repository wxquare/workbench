"""Admission owns default budget selection; runtime uses frozen Run limits."""
def has_capacity(limit:int,settled:int,reserved:int,requested:int,summary_reserve:int=0) -> bool:
    return settled+reserved+requested+summary_reserve <= limit
