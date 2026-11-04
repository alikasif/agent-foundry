"""Renders the reading list as a self-contained HTML email body (ED-2).

Role: builds the email-safe HTML from a ``ReadingList`` for the Gmail send tool.
Allowed project imports: ``render`` (``ENVIRONMENT`` and ``safe_url`` only) and
``models``. Only ``email_delivery`` imports this module.
"""
