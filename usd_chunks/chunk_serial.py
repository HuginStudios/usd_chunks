import json
import logging
import inspect
import importlib
from importlib import util
import sys
import weakref

import attrs
from attr import _make as attrMake
from functools import partial
from typing import Any, Dict, Callable, Sequence, Optional
# from collections import defaultdict

# from usd_chunks import chunk
# from usd_chunks.chunk_core import ChunkCommon, ChunkContainerBase, ChunkParamBase, NameSyncedContainer
# from usd_chunks.chunk_notice import ChunkNoticePropagator, NoticeConnection, ChunkNoticeAlias

logger = logging.getLogger(__name__)


class Serializeable:
    __slots__ = ()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def serialize(self) -> Optional[Dict]:
        fields = {}
        try:
            fields = attrs.fields_dict(type(self))
        except attrs.exceptions.NotAnAttrsClassError:
            pass

        res = {}
        # for name, val in inspect.getmembers(self, lambda x: not (inspect.ismethod(x) or inspect.isfunction(x) or inspect.classva)):

        if hasattr(self, "__dict__"):
            attributes = self.__dict__
        else:
            attributes = {x: getattr(self, x) for x in self.__slots__}

        for name, val in filter(lambda x: not (inspect.ismethod(x[1]) or inspect.isfunction(x[1])), attributes.items()):
            if name.startswith("_"):
                continue

            if field := fields.get(name):
                default = field.default
                if isinstance(default, attrMake.Factory):
                    if default.takes_self:
                        default = default.factory(self)
                    else:
                        default = default.factory()
                if val == default:
                    continue

            if isinstance(val, weakref.ReferenceType):
                val = val()
            res[name] = val

        res["class"] = f"{self.__module__}.{repr(self)}"
        res["hash"] = hash(self)
        return res


def serialize_Function(func: Callable):
    res: Dict[str, Any] = {}
    if inspect.ismethod(func):
        res["self"] = hash(func.__self__)
        res["function"] = func.__name__
    else:
        res["module"] = func.__module__
        # res["function"] = f"{func.__module__}.{func.__qualname__}"
        res["function"] = func.__qualname__
    return res


def serialize_CustomClass(custom_class):
    t = custom_class.__class__.__qualname__

    if hasattr(custom_class, "__dict__"):
        attributes = custom_class.__dict__.copy()
    else:
        attributes = {x: getattr(custom_class, x) for x in custom_class.__slots__}

    return {"custom_class": {"name": t, "module": custom_class.__module__, "attributes": attributes}}


def serialize_chunks(chunks: Sequence[Serializeable]):
    return json.dumps(chunks, cls=ChunkEncoder, indent=4)


class ChunkEncoder(json.JSONEncoder):
    def default(self, o: Any):
        if isinstance(o, Serializeable):
            return o.serialize()

        if callable(o):
            return serialize_Function(o)
        try:
            return super().default(o)
        except TypeError:
            return serialize_CustomClass(o)

        except Exception:
            logger.exception(f"Failed to encode: {o}")
            return {"failed": True}


def import_module(glb, module_name):
    spec = util.find_spec(module_name)
    if not spec:
        return

    importlib.invalidate_caches()
    module = util.module_from_spec(spec)
    if not module or not spec.loader:
        return
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception:
        # This module failed to load. Set to file_name so we can try loading it again later§
        return

    glb[module_name] = module
    return module


def decodeChunk(glb, data):
    if "__chunk_objects" not in glb:
        glb["__chunk_objects"] = {}

    if fn := data.get("function"):
        if fnself := data.get("self"):
            if obj := glb["__chunk_objects"].get(fnself):
                return getattr(obj, fn)
        else:
            mod_name = data.get("module")
            if not (module := import_module(glb, mod_name)):
                return
            return getattr(module, fn)

    if cnstr := data.get("class"):
        cls = eval(cnstr, glb)

        if h := data.get("hash"):
            glb["__chunk_objects"][h] = cls

    if custom_class := data.get("custom_class"):
        mod_name = custom_class.get("module")
        if not (module := import_module(glb, mod_name)):
            return

        cls = getattr(module, custom_class.get("name"))

        if h := data.get("hash"):
            glb["__chunk_objects"][h] = cls

        return cls(**custom_class.get("attributes"))

    return data


def deserialize_chunks(serial):
    if not __package__:
        return False

    importlib.invalidate_caches()
    glb = globals().copy()
    glb[__package__] = importlib.import_module(__package__)

    data = json.loads(serial, object_hook=partial(decodeChunk, glb))

    # if not (c := from_dict(data, glb, chunk.__dict__, objs)):
    #     return False
    # names = ChunkRegistry().get_chunk_names()

    # c.name = chunk.Chunk.get_valid_child_name(c.name, names)
    # self._add_chunk(c)
    return True


# def to_dict_ChunkModule(mod):
#     if not mod.loaded:
#         return None

#     res: Dict[str, Any] = {
#         "module_name": mod.module_name,
#         "module_file": mod.module_file.as_posix(),
#         "loaded": mod.loaded,
#     }
#     cs = []
#     if mod.loaded:
#         for i in mod.loaded_chunks.values():
#             cs.append(i.to_dict())
#     res["loaded_chunks"] = cs
#     return res


# def to_dict_NoticeConnection(conn: NoticeConnection):
#     method_name = ""
#     method_module = ""
#     method_self = None
#     if meth := conn.get_method():
#         method_module = meth.__module__

#         if inspect.ismethod(meth):
#             if isinstance(meth.__self__, ChunkNoticePropagator):
#                 method_self = hash(meth.__self__)
#             else:
#                 method_self = meth.__self__
#             method_name = f"{meth.__name__}"
#         else:
#             method_name = f"{meth.__name__}"

#     return {
#         "method": method_name,
#         "method_module": method_module,
#         "method_self": method_self,
#         "sender": hash(conn.sender) if conn.sender else None,
#         "owner": hash(conn.owner) if conn.owner else None,
#         "alias": conn.alias,
#         "globally": conn.globally,
#     }


# def to_dict_ChunkCommon(c: ChunkCommon):
#     res: Dict[str, Any] = {}
#     res["class"] = f"{c.__module__}.{repr(c)}"

#     cbs = []
#     for i in c._callbacks:
#         cbs.append(to_dict_NoticeConnection(i))

#     deps = []
#     for i in c.get_dependencies():
#         deps.append(hash(i))

#     funcs = []
#     for aname, afunc in c._attrib_functions.items():
#         func_self = None
#         if inspect.ismethod(afunc):
#             func_self = hash(afunc.__self__)
#         funcs.append((aname, afunc.__name__, func_self))

#     res["callbacks"] = cbs
#     res["dependencies"] = deps
#     res["hash"] = hash(c)
#     res["attrib_functions"] = funcs
#     return res


# def from_dict_NoticeConnection(cbinfo, objs, glb, loc=None):
#     method_str = cbinfo.get("method")
#     self_hash = cbinfo.get("method_self")
#     owner_hash = cbinfo.get("owner")
#     sender_hash = cbinfo.get("sender")

#     method = None
#     if self_hash:
#         if method_self := objs.get(self_hash):
#             return False, self_hash

#         method = getattr(method_self, method_str)
#     else:
#         try:
#             method = eval(method_str, glb, loc)
#         except Exception:
#             logger.exception(method_str)
#             return None, None

#     owner = None
#     if owner_hash and not (owner := objs.get(owner_hash)):
#         return False, owner_hash

#     sender = None
#     if sender_hash and not (sender := objs.get(sender_hash)):
#         return False, sender_hash

#     return True, NoticeConnection(
#         method, ChunkNoticeAlias(cbinfo.get("alias", 0)), sender, bool(cbinfo.get("globally")), owner
#     )


# def from_dict(data, glb=None, loc=None, objs=None):
#     cls_name = data.get("class")

#     if objs is None:
#         objs = {"chunks": {}, "waiting": defaultdict(dict)}

#     if "chunks" not in objs:
#         objs["chunks"] = {}

#     if "waiting" not in objs:
#         objs["waiting"] = defaultdict(dict)

#     try:
#         inst = eval(cls_name, glb, loc)
#     except Exception:
#         logger.exception(f"Failed to create from dict {cls_name}")
#         return None

#     oldhash = data.get("hash")
#     objs["chunks"][oldhash] = inst

#     if isinstance(inst, ChunkContainerBase):
#         inst.children.clear()
#         children = NameSyncedContainer()
#         children.set_owner(inst)
#         inst.children = children
#         for child_data in data.get("children", []):
#             if child := from_dict(child_data, glb, loc, objs):
#                 inst.children.add(child)

#     if isinstance(inst, ChunkParamBase):
#         inst.value = data.get("value")

#     if waiting := objs["waiting"].get(oldhash):
#         cbs = waiting.get("callbacks", [])
#         for i in reversed(range(len(cbs))):
#             succ, conn = from_dict_NoticeConnection(cbs[i], objs["chunks"], glb, loc)
#             if not succ or not conn:
#                 continue
#             inst.add_connection(cbs.pop(i))

#         deps = waiting.get("dependencies", [])
#         for i in reversed(range(len(deps))):
#             inst.add_dependency(deps.pop(i))

#         afuncs = waiting.get("attrib_functions", [])
#         for i in reversed(range(len(afuncs))):
#             aname, funcname, target = afuncs[i]
#             func = getattr(inst, funcname)
#             target.set_function(aname, func)

#     for cbinfo in data.get("callbacks", []):
#         succ, conn = from_dict_NoticeConnection(cbinfo, objs["chunks"], glb, loc)
#         if not succ:
#             if "callbacks" not in objs["waiting"][conn]:
#                 objs["waiting"][conn]["callbacks"] = []
#             objs["waiting"][conn]["callbacks"].append(cbinfo)
#             continue

#         if conn and not inst.find_callbacks(conn.get_method()):
#             inst.add_connection(conn)

#     for dephash in data.get("dependencies", []):
#         if not (dep := objs["chunks"].get(dephash)):
#             if "dependencies" not in objs["waiting"][dephash]:
#                 objs["waiting"][dephash]["dependencies"] = []
#             objs["waiting"][dephash]["dependencies"].append(inst)
#             continue
#         inst.add_dependency(dep)

#     for aname, funcname, func_self_hash in data.get("attrib_functions", []):
#         if func_self_hash:
#             if not (func_self := objs["chunks"].get(func_self_hash)):
#                 if "attrib_functions" not in objs["waiting"][func_self_hash]:
#                     objs["waiting"][func_self_hash]["attrib_functions"] = []
#                 objs["waiting"][func_self_hash]["attrib_functions"].append((aname, funcname, inst))
#                 continue
#             func = getattr(func_self, funcname)
#         else:
#             func = eval(funcname, glb, loc)

#         inst.set_function(aname, func)

#     return inst


# def to_dict(self):
#     method_name = ""
#     method_module = ""
#     method_self = None
#     if meth := self.get_method():
#         method_module = meth.__module__

#         if inspect.ismethod(meth):
#             if isinstance(meth.__self__, ChunkNoticePropagator):
#                 method_self = hash(meth.__self__)
#             else:
#                 method_self = meth.__self__
#             method_name = f"{meth.__name__}"
#         else:
#             method_name = f"{meth.__name__}"

#     return {
#         "method": method_name,
#         "method_module": method_module,
#         "method_self": method_self,
#         "sender": hash(self.sender) if self.sender else None,
#         "owner": hash(self.owner) if self.owner else None,
#         "alias": self.alias,
#         "globally": self.globally,
#     }

# def from_dict(cbinfo, objs, glb, loc=None):
#     method_str = cbinfo.get("method")
#     # method_module = cbinfo.get("method_module")

#     self_hash = cbinfo.get("method_self")
#     owner_hash = cbinfo.get("owner")
#     sender_hash = cbinfo.get("sender")

#     method = None
#     if self_hash:
#         if method_self := objs.get(self_hash):
#             return False, self_hash

#         method = getattr(method_self, method_str)
#     else:
#         try:
#             method = eval(method_str, glb, loc)
#         except Exception:
#             logger.exception(method_str)
#             return None, None

#     owner = None
#     if owner_hash and not (owner := objs.get(owner_hash)):
#         return False, owner_hash

#     sender = None
#     if sender_hash and not (sender := objs.get(sender_hash)):
#         return False, sender_hash

#     return True, NoticeConnection(
#         method, ChunkNoticeAlias(cbinfo.get("alias", 0)), sender, bool(cbinfo.get("globally")), owner
#     )
