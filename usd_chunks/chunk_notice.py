from pxr import Tf
from enum import IntEnum, auto
from typing import Callable, Any, List
from pxr.Usdviewq import usdviewApi as UsdviewModule
from pxr import Usd
from functools import partial

# Notice sent when Chunk parameter value was changed
class ParamChangeNotice(Tf.Notice):
    pass


class GroupChangeNotice(Tf.Notice):
    pass


class ChunkChangeNotice(Tf.Notice):
    pass


class ChunkLoadedNotice(Tf.Notice):
    pass


class PrimSelectionChangedNotice(Tf.Notice):
    pass


class PropSelectionChangedNotice(Tf.Notice):
    pass


Tf.Type.Define(ParamChangeNotice)
Tf.Type.Define(ChunkChangeNotice)
Tf.Type.Define(GroupChangeNotice)
Tf.Type.Define(ChunkLoadedNotice)
Tf.Type.Define(PrimSelectionChangedNotice)
Tf.Type.Define(PropSelectionChangedNotice)


class NoticeType(IntEnum):
    NONE = 0

    # Chunk parameters and value notices
    PARAM_CHANGED = auto()  # Called whenever a ChunkParam value changes
    GROUP_CHANGED = auto()  # Called when a group changes. This includes nested groups and param changes
    CHUNK_CHANGED = auto()  # Similar to GROUP_CHANGED but only for chunks

    # Stage notices
    PRIM_SELECTION_CHANGED = auto()  # Not implemented
    PROP_SELECTION_CHANGED = auto()  # Not implemented
    OBJECT_CHANGED = auto()  # Prim was changed on the stage

    # Registry notices
    CHUNK_LOADED = auto()  # Called when a new chunk has beed added to the registry


def get_notice(notice_type: NoticeType):
    if notice_type == NoticeType.NONE:
        return None
    if notice_type == NoticeType.PARAM_CHANGED:
        return ParamChangeNotice
    if notice_type == NoticeType.CHUNK_CHANGED:
        return ChunkChangeNotice
    if notice_type == NoticeType.GROUP_CHANGED:
        return GroupChangeNotice
    if notice_type == NoticeType.CHUNK_LOADED:
        return ChunkLoadedNotice

    if notice_type == NoticeType.PRIM_SELECTION_CHANGED:
        return PrimSelectionChangedNotice
    if notice_type == NoticeType.PROP_SELECTION_CHANGED:
        return PropSelectionChangedNotice
    if notice_type == NoticeType.OBJECT_CHANGED:
        return Usd.Notice.ObjectsChanged


def is_notice_type(notice: Tf.Notice, notice_type: NoticeType):
    return isinstance(notice, get_notice(notice_type))


class NoticeConnection:
    def __init__(self, method: Callable, notice_type: NoticeType, sender: Any = None, globally: bool = False, chunk_item = None):
        self.sender: Any = sender
        self.chunk_item: Any = chunk_item
        self.notice_type: NoticeType = notice_type
        self.listener: Tf.Notice.Listener = None
        self.globally: bool = globally
        self.method: Callable = method

        if chunk_item is None:
            self.__method = self.method
        else:
            self.__method = partial(self.method, self.chunk_item)

        self.connect()

    def connect(self, sender=None):
        notice_class = get_notice(self.notice_type)
        if not notice_class:
            print("Could not find appropriate notice to connect")
            return None

        if self.listener is not None:
            self.disconnect()

        if sender is not None:
            self.sender = sender

        if self.sender is None or self.globally:
            self.listener = Tf.Notice.RegisterGlobally(notice_class, self.__method)
        elif self.sender:
            self.listener = Tf.Notice.Register(notice_class, self.__method, self.sender)

    def disconnect(self):
        self.listener.Revoke()
        self.listener = None


class UsdviewNotice:
    def __init__(self, usdviewAPI: UsdviewModule.UsdviewApi):
        self.usdviewAPI: UsdviewModule.UsdviewApi = usdviewAPI

        data_model = self.usdviewAPI.dataModel

        selection_model = data_model.selection
        selection_model.signalPrimSelectionChanged.connect(self._primSelectionChanged)
        selection_model.signalPropSelectionChanged.connect(self._propSelectionChanged)

    @property
    def selectedPrims(self) -> List[Usd.Prim]:
        return self.usdviewAPI.selectedPrims

    @property
    def selectedProp(self):
        return self.usdviewAPI.property

    def _primSelectionChanged(self, *args, **kwargs):
        PrimSelectionChangedNotice().Send(self)

    def _propSelectionChanged(self, *args, **kwargs):
        PropSelectionChangedNotice().Send(self)
