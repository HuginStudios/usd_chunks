import griffe
from pathlib import Path
from inspect import isclass
import sys

import importlib.util

from attrs import fields_dict
import attr.exceptions

logger = griffe.get_logger("griffle_chunks")

class GriffleChunks(griffe.Extension):
    """An extension to inspect just a few specific objects."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.object_paths = paths if paths else []

    def on_object(
        self,
        obj: griffe.Object,
        loader: griffe.GriffeLoader,
        **kwargs,
    ) -> None:
        if obj.analysis == "dynamic":
            return  # Skip runtime objects, their docstrings are already right.

        if obj.path not in self.object_paths:
            return

        try:
            runtime_obj = griffe.dynamic_import(obj.path)

        except ImportError as error:
            logger.warning(f"Could not import {obj.path}: {error}")
            return

        if not isclass(runtime_obj):
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

            field_type = field.type.__name__ if field.type else None
            param = griffe.Parameter(field_name, annotation=field_type, default=field.default)
            
            # if doc := field.metadata.get("documentation"):

            member_docstring = griffe.Docstring("helloooo")
            # params.append(param)
            obj.set_member(field_name, griffe.Attribute(field_name, docstring=member_docstring))


        obj.set_member("__init__", griffe.Function("__init__", parameters=griffe.Parameters(*params)))
