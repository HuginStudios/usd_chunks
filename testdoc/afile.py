from attrs import field, define, Attribute

import sys
import os

from pxr import Usd

@define
class AClass:
    """A Class
    """

    some_field: int = field(default=0)
    some_other_field: int = field(default=0)


# @dataclass_transform(field_specifiers=(Attribute, field), eq_default=False)
# @dataclass_transform()
class AnotherClass:
    """
    Some docs
    """
    some_field: int = 1
    some_other_field: int = 1

    def __init__(self, A: int, B: str):
        """
        _summary_

        Args:
            A (int): _description_
            B (str): _description_
        """

