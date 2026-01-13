import griffe
from pathlib import Path
from inspect import isclass
from attr import _make as attrMake

from typing import get_type_hints, get_args, get_origin
from typing_extensions import reveal_type
# import importlib.util

from attrs import fields_dict, resolve_types
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
        if obj.analysis == "dynamic":
            return  # Skip runtime objects, their docstrings are already right.

        name = obj.path.split(".")[-2]
        if name not in self.modules:
            return

        try:
            runtime_obj = griffe.dynamic_import(obj.path)

        except ImportError as error:
            logger.warning(f"Could not import {obj.path}: {error}")
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

        logger.info(f"Parsing class {obj.path}")

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

            param = griffe.Parameter(field_name, annotation=str(field_type), default=default)
            params.append(param)

            # # if doc := field.metadata.get("documentation"):

            # member_docstring = griffe.Docstring("helloooo")
            # obj.set_member(field_name, griffe.Attribute(field_name, docstring=member_docstring))

        obj.set_member("__init__", griffe.Function("__init__", parameters=griffe.Parameters(*params)))    
