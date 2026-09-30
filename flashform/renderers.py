from rest_framework.renderers import BaseRenderer


class CSVRenderer(BaseRenderer):
    """Renders a CSV string as UTF-8 with a byte order mark: without it, Excel reads the
    file in the system code page and mangles accented names."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return ("﻿" + data).encode(self.charset)
