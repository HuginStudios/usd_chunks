# Plugins
Plugins refer to implementations of usd_chunks for specific applications. For example, UsdView can use the chunks with the chunk_usdview plugin. Any plugin may want to provide a ui 'plugin' as well, that can translate the chunks to specific ui frameworks. At the moment, the repository contains a plugin for Qt, but as the library is build with interpopability in mind, we aim to support more ui frameworks in the future.


# Implementing a UI plugin

Although the chunks works as-is with USD, a major benefit of the chunks are that they can easily be represented in graphically. The initial idea of the library was to have a simple way for users to create plugin for Usdview, without having to create actual plugins. The chunks themselves are, simply put, containers of data with connections to and from said data. Any modification to the data will send a notice, and any listener may react to the notices. This makes implementing a ui framework on top of the chunks straight-forward, as the ui framework just need to implement the widget logic and listen to some notices.

There are some chunk-specifics though that will need to be considered when implementing an external ui framework.

## Params

### ChunkParamWidget
Firstly, all 'widgets' representing a valued parameter (params subclassed from ChunkParamBase), should subclass chunk_widget_api.ChunkParamWidgetBase. This class provide some basic param<->widget communication, meaning setters and getters of the parameter. Here's an example for taken from chunk_qt, implementing the chunk.Splinbox parameter:

``` py

class ChunkFloatSpinboxWidget(ChunkParamWidgetBase[chunk.Spinbox], QtWidgets.QDoubleSpinBox):
    def __init__(self, param: chunk.Spinbox):
        super().__init__(param)

        self._step = 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(min_val * self._step)

        if max_val is not None:
            self.setMaximum(max_val * self._step)

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        return self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

```

the **setup_chunk_widget()** function is implemented by [ChunkParamWidgetBase][usd_chunks.chunk_widget_api.ChunkParamWidgetBase] and will call **set_value(), set_label(), set_icon()** and **set_set_param_layout()** respectively, each with the corresponding arguments taken from the param.

The get_* and set_* functions of a ChunkParamWidgetBase should set/get the value of the ui widget only. Any changes to the value of the widget should trigger **value_changed()** of ChunkParamWidgetBase. The **value_changed()** function is concrete and responsible for setting the value back to the actual parameter.

The handle to the chunk param is a weakref.ref() object, meaning it may be set to None in case the param has been deleted. Hence, it's advised to check if the param is None prior to doing anything with it. 

### ChunkWidgetFactoryBase
A ui implementation may want to create a subclass of [ChunkWidgetFactoryBase][usd_chunks.chunk_widget_api.ChunkWidgetFactoryBase] for mapping various params to their respective ui widgets. An example from chunk_qt:
``` py
class ChunkQTWidgetFactory(chunk_widget_api.ChunkWidgetFactoryBase):
    widget_map: Dict[ChunkFlags.ParamType, type[QtWidgets.QWidget]] = {
        ChunkFlags.ParamType.FLOAT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.INT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.FLOAT_SPINBOX: ChunkFloatSpinboxWidget,
        ChunkFlags.ParamType.INT_SPINBOX: ChunkIntSpinboxWidget,
        ChunkFlags.ParamType.LABEL: ChunkLabelWidget,
        ChunkFlags.ParamType.LINEEDIT: ChunkLineEditWidget,
        ChunkFlags.ParamType.FILE: ChunkFileWidget,
        ChunkFlags.ParamType.STRING_MENU: ChunkComboBoxWidget,
        ChunkFlags.ParamType.CHECKBOX: ChunkCheckBoxWidget,
        ChunkFlags.ParamType.BUTTON: ChunkPushButtonWidget,
        ChunkFlags.ParamType.SEPARATOR: ChunkSeparatorWidget,
    }

    def create_widget(self, param: chunk.ChunkParamBase) -> ChunkParamWidget | None:
        """Create a Qt widget based on the ChunkParam object. The param.get_type() should determine the type of Qt Widget to create

        Args:
            param (chunk.ChunkParamBase): The parameter. The widget will update param.value when widget value is changed

        Returns:
            QtWidgets.QWidget: The QtWidget created
            None: If failed to create a widget
        """
        key = param.evaluate_type()
        widget_class = self.get_map().get(key, None)

        if not widget_class:
            logger.warning("Could not create widget for param %s: missing mapping in Qt factory", param.name)
            return None

        widget: ChunkParamWidget = widget_class(param)

        if param.tooltip and isinstance(widget, QtWidgets.QWidget):
            widget.setToolTip(param.tooltip)

        return widget
```

Using this (or any other factory if you prefer), enables easy creation of the widgets from params:
``` py
widget = self.factory.create_widget(param)
```

A chunk Group parameter have an optional widget_factory member, that can dictate which widget_factory to use. For example, if a chunk group wishes to have special widgets for it's parameters, the ui plugin should respect this.

## Groups

All chunk objects (chunks, groups, params) are dynamic, meaning they may be created or deleted at runtime. This should be taken in to consideration when implementing a ui plugin. The notice system can help with this, as relevant creation / deletion notices are sent whenever a chunk object should be added or removed from the ui. 

### ChunkContainerWidgetBase
Similarly to the ChunkParamWidget, group widget classes can subclass the abstract [ChunkContainerWidgetBase][usd_chunks.chunk_core.ChunkContainerWidgetBase] class which provides default implementations for much of the chunk->widget communication. However, any ui library specific functionality need be implemented by the ui plugin. See the chunk_qt ContainerWidget for an example.

## CHUNK_LOADED
A ui implementation may listen to the creation of chunks using the CHUNK_LOADED notice. When received, the ui implementation should create the chunk and all its parameters. 

The ChunkWidgetDocker of the chunk_qt plugin listen to this notice, and will create the chunk widgets whenever the notice is received. A similar implementation may be relevant in other ui plugins as well.


# Application plugins
Host entry points live in `usd_chunks.hosts`. Currently, we provide a plugin for Usdview only. The Kraken host may be expanded on later, but is currently in internal development.

Application plugins will vary in their implementations as the applications will load plugins differently. In some cases an application plugin may not be necessary at all.

In any case, using the chunks for any application require some initialization from the chunk_registry module. In the Usdview plugin, we do this inside of the **ChunksPluginContainer**.


``` py
...
    scene = UsdviewScene(usdviewApi)

    chunk_qt.build_chunk_docker(usdviewApi.qMainWindow)
    chunk_registry.ChunkLayerManager().initialize(usdviewApi.stage)
    chunk_registry.ChunkRegistry().initialize(scene, "chunk_usdview")
```

The UsdviewScene is an implementation of the abstract [SceneSender][usd_chunks.chunk_scene.SceneSender] class, which provides access to the scene as well as sends notices upon selection changes. Any application plugin should provide a similar scene proxy. This is important as some chunks will rely on prim selections, or other scene specific functionality. The scene proxy also provide a handle to the UsdStage, which the underlying application usually provides.

The ChunkRegistry is responsible for finding and loading any chunks in the environment, and the ChunkLayerManager is responsible for managing the SdfLayers of the chunks. Both of these need to be initialized in order to get proper functionality

In the case of the usdview plugin, we can use the chunk_qt ui plugin, as usdview uses Qt as well. This is expressed simply by calling the **chunk_qt.build_chunk_docker** function (`chunk_qt` being `usd_chunks.frontends.qt.factory`). For other applications not using Qt, an additional ui plugin would be needed to get the param widgets
