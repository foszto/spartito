"""The orchestrator's core behind the facade (service.Orchestrator).

Each module holds methods of the facade as functions whose first parameter is the facade
itself (`orch`, typed by the module's `Facade` protocol); service.Orchestrator binds them as
its methods. They reach other methods and the store through `orch`, as they did through
`self`, so a method replaced on the class or the instance (`_launch` in tests) still takes
effect everywhere. The exception is what a runner process uses without the facade: the run-end
steps (lifecycle), the automatic compaction policy and starting a runner (launcher.launch) take
the store and a `launch` instead; the facade's methods delegate to them. No core module imports
the service or the runner.
"""
