"""A griffe extension that gives the attrs-built chunk classes their real __init__ signatures.

The chunk classes get __init__ from attrs at runtime, so static analysis sees none; this
reads the fields back so the API docs show the constructor.
"""
from __future__ import annotations

import griffe
from inspect import isclass
from attr import _make as attrMake


from attrs import fields_dict
import attr.exceptions

logger = griffe.get_logger("griffle_chunks")


class GriffleChunks(griffe.Extension):
    """An extension to inspect just a few specific objects."""

    def __init__(self, modules: list[str] | None = None) -> None:
        self.modules = set(modules) if modules else set()

    def on_object(
        self,
        obj: griffe.Object,
        loader: griffe.GriffeLoader,
        **kwargs,
    ) -> None:
        """Add the attrs-generated __init__ signature to a chunk class from the configured modules.

        Args:
            obj: The object griffe loaded.
            loader: The griffe loader.
            **kwargs: Other arguments griffe passes.
        """
        if obj.analysis == "dynamic":
            return  # Skip runtime objects, their docstrings are already right.

        name = obj.path.split(".")[-2]
        if name not in self.modules:
            return

        try:
            runtime_obj = griffe.dynamic_import(obj.path)
        except ImportError as error:
            logger.warning("Could not import %s: %s", obj.path, error)
            return

        if not isclass(runtime_obj):
            print("not class", obj.path)
            return

        if obj.members.get("__init__"):
            return
        try:
            fields = fields_dict(runtime_obj).items()
        except attr.exceptions.NotAnAttrsClassError:
            return

        logger.info("Parsing class %s", obj.path)

        params = []
        for field_name, field in fields:
            if not field.init:
                continue

            field_type = field.type if field.type else None

            if isclass(field_type):
                field_type = field_type.__name__

            default = field.default
            if isinstance(default, attrMake.Factory):
                default = default.factory()

            if isinstance(default, str):
                default = f"'{default}'"

            param = griffe.Parameter(field_name, annotation=str(field_type), default=str(default))
            params.append(param)

        obj.set_member("__init__", griffe.Function("__init__", parameters=griffe.Parameters(*params)))
