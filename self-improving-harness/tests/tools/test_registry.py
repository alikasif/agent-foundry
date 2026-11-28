from pathlib import Path

from google.adk.tools.base_tool import BaseTool

from harness.prompts.load import load_tool_descriptions
from harness.sandbox.container import Container, ContainerSpec
from harness.sandbox.interpreter import Interpreter
from harness.tools.plan.tool import PlanState
from harness.tools.registry import build_tools


def tools(folder: Path) -> dict[str, BaseTool]:
    container = Container(ContainerSpec("image", folder))
    built = build_tools(
        container, Interpreter(container), PlanState(), load_tool_descriptions()
    )
    return {tool.name: tool for tool in built}


def parameters(tool: BaseTool) -> dict:
    declaration = tool._get_declaration()
    assert declaration is not None
    schema = declaration.parameters_json_schema
    assert isinstance(schema, dict)
    return schema


def test_names(tmp_path: Path) -> None:
    assert list(tools(tmp_path)) == ["plan", "bash", "python"]


def test_declarations_carry_the_toml_descriptions(tmp_path: Path) -> None:
    descriptions = load_tool_descriptions()

    for name, tool in tools(tmp_path).items():
        declaration = tool._get_declaration()
        assert declaration is not None
        assert declaration.description == descriptions[name]


def test_code_tools_take_one_string(tmp_path: Path) -> None:
    for name, argument in (("bash", "command"), ("python", "code")):
        schema = parameters(tools(tmp_path)[name])

        assert list(schema["properties"]) == [argument]
        assert schema["properties"][argument]["type"] == "string"
        assert schema["required"] == [argument]


def test_plan_parameters(tmp_path: Path) -> None:
    schema = parameters(tools(tmp_path)["plan"])
    properties = schema["properties"]

    assert list(properties) == ["understanding", "steps", "done"]
    assert properties["understanding"]["type"] == "string"
    assert properties["steps"]["type"] == "array"
    assert properties["steps"]["items"] == {"type": "string"}
    assert properties["done"]["type"] == "integer"
    assert schema["required"] == ["understanding", "steps"]
