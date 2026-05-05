from rne.models import HandbrakeArgs


def build_command(source_path: str, output_path: str, args: HandbrakeArgs) -> list[str]:
    raise NotImplementedError
