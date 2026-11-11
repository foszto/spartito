"""The orchestrator's core behind the facade (service.Orchestrator).

Each module holds methods of the facade as functions whose first parameter is the facade
itself (`orch`, typed by the module's `Facade` protocol); service.Orchestrator binds them as
its methods. They reach other methods and the store through `orch`, as they did through
`self`, so a method replaced on the class or the instance (`_launch` in tests) still takes
effect everywhere. No core module imports the service.
"""
