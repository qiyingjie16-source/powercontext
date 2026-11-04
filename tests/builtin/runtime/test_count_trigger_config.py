# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Public count-trigger configuration bounds."""

import pytest
from pydantic import ValidationError

from powercontext.builtin.runtime.config import RuntimeConfig


@pytest.mark.parametrize("field", ["memory_source_count_threshold", "topic_memory_source_count_threshold"])
def test_count_threshold_is_optional_strict_positive_integer(field):
    assert getattr(RuntimeConfig(), field) is None
    for value in (None, 1, 7, 100):
        assert getattr(RuntimeConfig(**{field: value}), field) == value
    for value in (True, False, 0, -1, 1.0, "3"):
        with pytest.raises(ValidationError):
            RuntimeConfig(**{field: value})
