"""Scanners for the Python web frameworks: FastAPI, Flask, and Django."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Iterator

from .base import (
    DiscoveredEndpoint,
    ScanResult,
    action_from,
    canonical_path,
    controller_from_path,
    iter_source_files,
    normalise_path,
    path_parameters,
    read_text,
    relative_to,
    titleize,
)


METHOD_DECORATORS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
}

#: Parameter annotations that are request context, not client input.
FRAMEWORK_TYPES = {
    "Request",
    "Response",
    "BackgroundTasks",
    "WebSocket",
    "Session",
    "Depends",
    "SecurityScopes",
}

#: Annotations that describe an untyped body rather than a request model.
GENERIC_ANNOTATIONS = {"dict", "list", "Dict", "List", "Any", "object", "bytes"}

SCALAR_ANNOTATIONS = {    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "UUID": "string",
    "date": "string",
    "datetime": "string",
    "Decimal": "number",
}


def _parse(path: Path) -> ast.Module | None:
    text = read_text(path)
    if not text:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _decorator_parts(node: ast.expr) -> tuple[str, str] | None:
    """Returns ``(object_name, attribute)`` for ``@app.get`` style decorators."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return target.value.id, target.attr
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute):
        return target.value.attr, target.attr
    return None


def _first_string_argument(node: ast.Call) -> str:
    for argument in node.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
    for keyword in node.keywords:
        if keyword.arg in {"path", "rule", "route"} and isinstance(
            keyword.value, ast.Constant
        ):
            return str(keyword.value.value)
    return ""


def _keyword_list(node: ast.Call, name: str) -> list[str]:
    for keyword in node.keywords:
        if keyword.arg == name and isinstance(keyword.value, (ast.List, ast.Tuple)):
            return [
                str(element.value)
                for element in keyword.value.elts
                if isinstance(element, ast.Constant)
            ]
    return []


def _annotation_name(annotation: ast.expr | None) -> str:
    if annotation is None:
        return ""
    if isinstance(annotation, ast.Call):
        return _annotation_name(annotation.func)
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value
    if isinstance(annotation, ast.BinOp):
        # ``str | None`` keeps the left-hand concrete type.
        return _annotation_name(annotation.left)
    return ""


def _subscript_arguments(annotation: ast.Subscript) -> list[ast.expr]:
        value = annotation.slice
        return list(value.elts) if isinstance(value, ast.Tuple) else [value]


def _schema_from_annotation(
        annotation: ast.expr | None,
        models: dict[str, dict[str, Any]],
        seen: set[str] | None = None,
) -> dict[str, Any] | None:
        """Converts Python type annotations into the catalog field-tree format."""
        if annotation is None:
            return None
        if isinstance(annotation, ast.Call):
            schema = _schema_from_annotation(annotation.func, models, seen)
            if schema and bool(_keyword_constant(annotation, "many", False)):
                return {
                    "kind": "array",
                    "clr_type": f"list[{schema.get('clr_type', 'object')}]",
                    "item": schema,
                }
            return schema
        seen = set() if seen is None else set(seen)
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            candidates = [annotation.left, annotation.right]
            concrete = next(
                (
                    item
                    for item in candidates
                    if _annotation_name(item) not in {"None", "NoneType"}
                ),
                annotation.left,
            )
            schema = _schema_from_annotation(concrete, models, seen)
            if schema:
                schema["nullable"] = True
            return schema
        if isinstance(annotation, ast.Subscript):
            outer = _annotation_name(annotation.value)
            arguments = _subscript_arguments(annotation)
            if outer in {"Optional", "Union"}:
                concrete = next(
                    (
                        item
                        for item in arguments
                        if _annotation_name(item) not in {"None", "NoneType"}
                    ),
                    None,
                )
                schema = _schema_from_annotation(concrete, models, seen)
                if schema:
                    schema["nullable"] = True
                return schema
            if outer in {"list", "List", "Sequence", "Iterable", "set", "tuple"}:
                item = _schema_from_annotation(arguments[0], models, seen) if arguments else None
                return {
                    "kind": "array",
                    "clr_type": ast.unparse(annotation),
                    "item": item or {"kind": "text", "clr_type": "str"},
                }
            if outer in {"Literal"}:
                values = [
                    str(item.value)
                    for item in arguments
                    if isinstance(item, ast.Constant)
                ]
                return {"kind": "enum", "clr_type": "Literal", "lov": values}
            if outer in {"dict", "Dict", "Mapping"}:
                return {"kind": "json", "clr_type": ast.unparse(annotation)}
        name = _annotation_name(annotation)
        scalar = {
            "str": "text",
            "UUID": "guid",
            "date": "date",
            "datetime": "date",
            "int": "integer",
            "float": "number",
            "Decimal": "number",
            "bool": "boolean",
            "dict": "json",
            "Any": "json",
        }.get(name)
        if scalar:
            return {"kind": scalar, "clr_type": name}
        if name in models and name not in seen:
            schema = models[name]
            return {
                **schema,
                "fields": [dict(item) for item in schema.get("fields", [])],
            }
        return None


def _call_name(node: ast.expr | None) -> str:
        if isinstance(node, ast.Call):
            return _call_name(node.func)
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""


def _keyword_constant(call: ast.Call, name: str, default: Any = None) -> Any:
        for keyword in call.keywords:
            if keyword.arg == name and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
        return default


def _module_models(tree: ast.Module) -> dict[str, dict[str, Any]]:
        """Reads Pydantic/dataclass/TypedDict and DRF/Marshmallow schema classes."""
        classes = {
            node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        }
        models: dict[str, dict[str, Any]] = {}
        # Seed names first so nested and mutually-referenced annotations resolve.
        for name in classes:
            models[name] = {"kind": "object", "name": name, "clr_type": name, "fields": []}
        for name, node in classes.items():
            fields: list[dict[str, Any]] = []
            for child in node.body:
                field_name = ""
                annotation: ast.expr | None = None
                default: ast.expr | None = None
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    field_name = child.target.id
                    annotation = child.annotation
                    default = child.value
                elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(
                    child.targets[0], ast.Name
                ) and isinstance(child.value, ast.Call):
                    field_name = child.targets[0].id
                    default = child.value
                    field_type = _call_name(child.value)
                    annotation = ast.Name(
                        id={
                            "CharField": "str",
                            "EmailField": "str",
                            "SlugField": "str",
                            "UUIDField": "UUID",
                            "IntegerField": "int",
                            "FloatField": "float",
                            "DecimalField": "Decimal",
                            "BooleanField": "bool",
                            "DateField": "date",
                            "DateTimeField": "datetime",
                            "DictField": "dict",
                            "JSONField": "dict",
                        }.get(field_type, field_type or "str")
                    )
                if not field_name or field_name.startswith("_"):
                    continue
                schema = _schema_from_annotation(annotation, models) or {
                    "kind": "text",
                    "clr_type": _annotation_name(annotation) or "str",
                }
                required = default is None
                nullable = False
                if isinstance(default, ast.Call):
                    required = bool(_keyword_constant(default, "required", True))
                    nullable = bool(
                        _keyword_constant(default, "allow_null", False)
                        or _keyword_constant(default, "allow_none", False)
                    )
                    many = bool(_keyword_constant(default, "many", False))
                    if many:
                        schema = {
                            "kind": "array",
                            "clr_type": f"list[{schema.get('clr_type', 'object')}]",
                            "item": schema,
                        }
                elif isinstance(default, ast.Constant):
                    required = default.value is Ellipsis
                    nullable = default.value is None
                schema.update(
                    {
                        "name": field_name,
                        "display_name": field_name,
                        "required": required,
                        "nullable": nullable or bool(schema.get("nullable")),
                    }
                )
                fields.append(schema)
            models[name]["fields"] = fields
        return {
            name: schema
            for name, schema in models.items()
            if schema.get("fields")
        }


def _response_annotation(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        decorator: ast.Call | None,
        framework: str,
) -> ast.expr | None:
        if decorator is not None and framework == "fastapi":
            for keyword in decorator.keywords:
                if keyword.arg == "response_model":
                    return keyword.value
        if decorator is not None and framework == "flask":
            for nested in function.decorator_list:
                if isinstance(nested, ast.Call) and _call_name(nested) in {
                    "marshal_with",
                    "response",
                    "output",
                } and nested.args:
                    return nested.args[-1]
        return function.returns


def _default_marker(default: ast.expr | None) -> str:
    """Identifies FastAPI markers such as ``Query(...)`` or ``Body(...)``."""
    if isinstance(default, ast.Call):
        function = default.func
        if isinstance(function, ast.Name):
            return function.id
        if isinstance(function, ast.Attribute):
            return function.attr
    return ""


def _function_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    path: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Splits a handler signature into catalog parameters and a payload type."""
    known_path = {item["name"] for item in path_parameters(path)}
    parameters: list[dict[str, Any]] = []
    payload_type: str | None = None

    arguments = list(function.args.args) + list(function.args.kwonlyargs)
    defaults: list[ast.expr | None] = [None] * (
        len(function.args.args) - len(function.args.defaults)
    ) + list(function.args.defaults)
    defaults += list(function.args.kw_defaults)

    for index, argument in enumerate(arguments):
        name = argument.arg
        if name in {"self", "cls", "request", "response"}:
            continue
        annotation = _annotation_name(argument.annotation)
        if annotation in FRAMEWORK_TYPES:
            continue
        default = defaults[index] if index < len(defaults) else None
        marker = _default_marker(default)
        if marker == "Depends":
            continue

        if name in known_path or marker == "Path":
            source = "path"
        elif marker in {"Body", "Form", "File", "UploadFile"}:
            source = "form" if marker in {"Form", "File"} else "body"
        elif annotation == "UploadFile":
            source = "form"
        elif marker == "Header":
            source = "header"
        elif annotation and annotation not in SCALAR_ANNOTATIONS and marker != "Query":
            # An unrecognised class annotation is a request model.
            source = "body"
        else:
            source = "query"

        if source == "body":
            # ``dict``/``list`` annotations name no schema, so leave the type
            # unset and let the builder show a free-form JSON body instead.
            payload_type = (
                annotation
                if annotation and annotation not in GENERIC_ANNOTATIONS
                else payload_type
            )
            continue

        parameters.append(
            {
                "name": name,
                "source": source,
                "type": SCALAR_ANNOTATIONS.get(annotation, annotation or "string"),
                "required": default is None and source != "query",
            }
        )

    for item in path_parameters(path):
        if not any(
            existing["name"] == item["name"] and existing["source"] == "path"
            for existing in parameters
        ):
            parameters.append(item)
    return parameters, payload_type


def _iter_functions(
    tree: ast.Module,
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Yields every function with the enclosing class name, if any."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, ""
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, node.name


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Maps each router/blueprint variable to its declared path prefix.

    The prefix belongs to the variable it was declared on, so a route on
    ``@app.get`` must not inherit the prefix of an ``APIRouter`` that happens
    to live in the same module.
    """
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        function = node.value.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name not in {"APIRouter", "Blueprint"}:
            continue
        prefix = ""
        for keyword in node.value.keywords:
            if keyword.arg in {"prefix", "url_prefix"} and isinstance(
                keyword.value, ast.Constant
            ):
                prefix = str(keyword.value.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _decorated_routes(
    root: Path,
    service_name: str,
    framework: str,
) -> ScanResult:
    """Shared implementation for FastAPI and Flask decorator routing."""
    result = ScanResult(framework=framework, root=root)
    for file_path in iter_source_files(root, {".py"}):
        tree = _parse(file_path)
        if tree is None:
            continue
        prefixes = _router_prefixes(tree)
        models = _module_models(tree)
        relative = relative_to(file_path, root)
        for function, class_name in _iter_functions(tree):
            for decorator in function.decorator_list:
                parts = _decorator_parts(decorator)
                if parts is None:
                    continue
                holder, attribute = parts
                call = decorator if isinstance(decorator, ast.Call) else None
                if call is None:
                    continue

                if attribute in METHOD_DECORATORS:
                    methods = [attribute.upper()]
                elif attribute == "route":
                    methods = [
                        item.upper() for item in _keyword_list(call, "methods")
                    ] or ["GET"]
                elif attribute in {"api_route", "add_api_route"}:
                    methods = [
                        item.upper() for item in _keyword_list(call, "methods")
                    ] or ["GET"]
                else:
                    continue

                route = _first_string_argument(call)
                if not route:
                    continue
                full_path = canonical_path(prefixes.get(holder, ""), route)
                parameters, payload_type = _function_parameters(function, full_path)
                response_annotation = _response_annotation(
                    function, call, framework
                )
                response_schema = _schema_from_annotation(
                    response_annotation, models
                )
                response_name = _annotation_name(response_annotation)
                if response_schema and response_schema.get("kind") == "array":
                    item = response_schema.get("item") or {}
                    response_name = str(item.get("name") or item.get("clr_type") or response_name)
                    response_name = f"{response_name}List"
                if response_schema and response_name:
                    response_schema = {
                        **response_schema,
                        "name": response_name,
                        "clr_type": ast.unparse(response_annotation)
                        if response_annotation is not None
                        else response_name,
                    }
                    result.response_schemas.setdefault(response_name, response_schema)
                controller = (
                    titleize(class_name.removesuffix("View").removesuffix("Resource"))
                    if class_name
                    else controller_from_path(full_path, titleize(file_path.stem))
                )
                for method in methods:
                    if method not in {
                        "GET",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "HEAD",
                        "OPTIONS",
                    }:
                        continue
                    result.endpoints.append(
                        DiscoveredEndpoint(
                            service=service_name,
                            controller=controller or "Root",
                            action=action_from(method, full_path, function.name),
                            method=method,
                            path=full_path,
                            parameters=[dict(item) for item in parameters],
                            payload_type=payload_type,
                            response_schema=response_name if response_schema else None,
                            source_file=relative,
                            source_line=function.lineno,
                        )
                    )
    return result


def scan_fastapi(root: Path, service_name: str) -> ScanResult:
    result = _decorated_routes(root, service_name, "fastapi")
    if not result.endpoints:
        result.warnings.append(
            "No @app/@router HTTP decorators were found. Check that the scan "
            "root contains the FastAPI application package."
        )
    return result


def scan_flask(root: Path, service_name: str) -> ScanResult:
    result = _decorated_routes(root, service_name, "flask")
    if not result.endpoints:
        result.warnings.append(
            "No @app.route or @blueprint.route decorators were found."
        )
    return result


def _django_view_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == "as_view":
            return _django_view_name(function.value)
        return _django_view_name(function)
    return ""


#: Actions generated by a Django REST Framework router registration.
VIEWSET_ROUTES = (
    ("GET", "", "List"),
    ("POST", "", "Create"),
    ("GET", "{id}", "Retrieve"),
    ("PUT", "{id}", "Update"),
    ("PATCH", "{id}", "PartialUpdate"),
    ("DELETE", "{id}", "Destroy"),
)


def scan_django(root: Path, service_name: str) -> ScanResult:
    """Reads ``urls.py`` modules, including DRF router registrations."""
    result = ScanResult(framework="django", root=root)
    models: dict[str, dict[str, Any]] = {}
    serializer_for_view: dict[str, str] = {}
    for source in iter_source_files(root, {".py"}):
        source_tree = _parse(source)
        if source_tree is None:
            continue
        models.update(_module_models(source_tree))
        for class_node in (
            item for item in source_tree.body if isinstance(item, ast.ClassDef)
        ):
            for child in class_node.body:
                if (
                    isinstance(child, ast.Assign)
                    and len(child.targets) == 1
                    and isinstance(child.targets[0], ast.Name)
                    and child.targets[0].id == "serializer_class"
                ):
                    serializer_for_view[class_node.name] = _annotation_name(child.value)
    url_files = [
        path
        for path in iter_source_files(root, {".py"})
        if path.name in {"urls.py", "routers.py", "api_urls.py"}
    ]
    for file_path in url_files:
        tree = _parse(file_path)
        if tree is None:
            continue
        relative = relative_to(file_path, root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else getattr(function, "attr", "")
            )

            if name in {"path", "re_path", "url"} and node.args:
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(
                    first.value, str
                ):
                    continue
                route = canonical_path(first.value)
                view = _django_view_name(node.args[1]) if len(node.args) > 1 else ""
                controller = (
                    titleize(view.removesuffix("View").removesuffix("APIView"))
                    or controller_from_path(route, titleize(file_path.parent.name))
                )
                serializer_name = serializer_for_view.get(view)
                response_schema = models.get(serializer_name or "")
                if serializer_name and response_schema:
                    result.response_schemas.setdefault(
                        serializer_name, response_schema
                    )
                for method in ("GET", "POST"):
                    result.endpoints.append(
                        DiscoveredEndpoint(
                            service=service_name,
                            controller=controller or "Root",
                            action=action_from(method, route, view),
                            method=method,
                            path=route,
                            parameters=path_parameters(route),
                            response_schema=serializer_name
                            if response_schema and method == "GET"
                            else None,
                            source_file=relative,
                            source_line=node.lineno,
                        )
                    )

            elif name == "register" and node.args:
                prefix_node = node.args[0]
                if not isinstance(prefix_node, ast.Constant):
                    continue
                prefix = str(prefix_node.value)
                view = _django_view_name(node.args[1]) if len(node.args) > 1 else ""
                controller = titleize(view.removesuffix("ViewSet")) or titleize(prefix)
                serializer_name = serializer_for_view.get(view)
                response_schema = models.get(serializer_name or "")
                if serializer_name and response_schema:
                    result.response_schemas.setdefault(
                        serializer_name, response_schema
                    )
                for method, suffix, action in VIEWSET_ROUTES:
                    route = canonical_path(prefix, suffix)
                    endpoint_schema = serializer_name if response_schema else None
                    if endpoint_schema and action == "List":
                        list_name = f"{serializer_name}List"
                        result.response_schemas.setdefault(
                            list_name,
                            {
                                "kind": "array",
                                "name": list_name,
                                "clr_type": f"list[{serializer_name}]",
                                "item": response_schema,
                            },
                        )
                        endpoint_schema = list_name
                    result.endpoints.append(
                        DiscoveredEndpoint(
                            service=service_name,
                            controller=controller or "Root",
                            action=f"{controller}{action}",
                            method=method,
                            path=route,
                            parameters=path_parameters(route),
                            response_schema=endpoint_schema
                            if method in {"GET", "POST", "PUT", "PATCH"}
                            else None,
                            source_file=relative,
                            source_line=node.lineno,
                        )
                    )

    if not url_files:
        result.warnings.append(
            "No urls.py was found. Point the scan at the Django project root."
        )
    elif not result.endpoints:
        result.warnings.append(
            "urls.py files were found but no path()/register() calls could be read."
        )
    else:
        result.warnings.append(
            "Django function views expose GET and POST by default; remove any "
            "method your view does not implement."
        )
    return result
