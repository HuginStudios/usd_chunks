import re


def get_unique_path(stage, prim_path):
    if not stage.GetPrimAtPath(prim_path).IsValid():
        return prim_path

    # Strip any trailing digits to get the base
    base = re.sub(r'\d+$', '', prim_path)

    count = 1
    while stage.GetPrimAtPath (f"{base}{count}").IsValid():
        count += 1

    return f"{base}{count}"