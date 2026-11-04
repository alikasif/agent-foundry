"""Orchestrates the email send after a digest run (ED-2, ED-16).

Role: glue between the finished reading list and the Gmail MCP send tool.
Allowed project imports: ``gmail_mcp`` (send names only), ``email_render``, ``models``
and ``config``. Only ``cli`` imports this module, lazily and only with ``--email``.
"""
