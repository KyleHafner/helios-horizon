import game_control.introspection as introspection


def test_signature_parameters_caches_plain_function(monkeypatch):
    calls = 0
    original = introspection.inspect.signature

    def counted(function):
        nonlocal calls
        calls += 1
        return original(function)

    monkeypatch.setattr(introspection.inspect, "signature", counted)

    def seam(first, second=None):
        return first, second

    assert [item.name for item in introspection.signature_parameters(seam)] == [
        "first",
        "second",
    ]
    assert introspection.signature_parameters(seam) == introspection.signature_parameters(seam)
    assert calls == 1


def test_signature_parameters_caches_bound_function_without_retaining_bound_method(
    monkeypatch,
):
    calls = 0
    original = introspection.inspect.signature

    def counted(function):
        nonlocal calls
        calls += 1
        return original(function)

    monkeypatch.setattr(introspection.inspect, "signature", counted)

    class Service:
        def seam(self, request, actor=None):
            return request, actor

    service = Service()
    assert [item.name for item in introspection.signature_parameters(service.seam)] == [
        "request",
        "actor",
    ]
    assert [item.name for item in introspection.signature_parameters(service.seam)] == [
        "request",
        "actor",
    ]
    assert calls == 1


def test_signature_parameters_supports_static_class_and_callable_variants(monkeypatch):
    class Service:
        @staticmethod
        def static(request, actor=None):
            return request, actor

        @classmethod
        def class_method(cls, request, actor=None):
            return cls, request, actor

    class CallableService:
        def __call__(self, request, actor=None):
            return request, actor

    assert [p.name for p in introspection.signature_parameters(Service.static)] == ["request", "actor"]
    assert [p.name for p in introspection.signature_parameters(Service.class_method)] == ["request", "actor"]

    calls = 0
    original = introspection.inspect.signature

    def counted(function):
        nonlocal calls
        calls += 1
        return original(function)

    monkeypatch.setattr(introspection.inspect, "signature", counted)
    callable_service = CallableService()
    assert [p.name for p in introspection.signature_parameters(callable_service)] == ["request", "actor"]
    assert [p.name for p in introspection.signature_parameters(callable_service)] == ["request", "actor"]
    # Callable instances are intentionally not cached: caching them would retain
    # injected service instances, while function seams are safely bounded.
    assert calls == 2


def test_signature_cache_evicts_old_function_keys(monkeypatch):
    original = introspection.inspect.signature
    calls = 0

    def counted(function):
        nonlocal calls
        calls += 1
        return original(function)

    monkeypatch.setattr(introspection.inspect, "signature", counted)
    functions = []
    for index in range(257):
        namespace = {}
        exec(f"def seam(value_{index}): return value_{index}", namespace)
        functions.append(namespace["seam"])
    for function in functions:
        introspection.signature_parameters(function)
    introspection.signature_parameters(functions[0])
    assert calls == 258
