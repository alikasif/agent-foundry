from harness.tools.described import DescribedTool


async def bash(command: str) -> dict:
    """Not what the model should read."""
    return {}


def test_the_description_comes_from_the_argument_every_time() -> None:
    tool = DescribedTool(bash, "Run it.")

    for _ in range(2):
        declaration = tool._get_declaration()
        assert declaration.description == "Run it."
    assert tool.name == "bash"
    assert tool.description == "Run it."
