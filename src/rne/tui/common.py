"""Styles shared by the rne terminal apps."""

APP_CSS = """
Screen { background: $surface; }
.section { text-style: bold; margin-top: 1; color: $accent; }
.hint { color: $text-muted; }
.field { height: auto; }
.field Label { width: 16; }
.field Input { width: 50; }
.field Input.number { width: 8; }
.field Input { background: $boost; }
.field Input:focus { background: $primary 30%; }
#errors { color: $error; margin-top: 1; }
#outputs { color: $text-muted; }
#buttons { height: auto; margin-top: 1; }
#buttons Button { margin-right: 2; }
#body { padding: 0 2; }
.muted { color: $text-muted; }
.ok { color: $success; }
.warn { color: $warning; }
DataTable { height: auto; max-height: 20; }
"""
