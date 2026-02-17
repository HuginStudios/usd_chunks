from pathlib import Path

def get_resource_path():
    return Path(__file__).parent.parent / "resources"

def get_icons_path():
    return get_resource_path() / "icons"

def get_icon(name, ext=".png"):
    res =  Path(get_icons_path() / name).with_suffix(ext)
    return res

