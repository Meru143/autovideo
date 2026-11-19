"""The operator console (design: docs/superpowers/specs/2026-11-09-operator-console-design.md; plan:
docs/superpowers/plans/2026-11-09-operator-console.md): a local JSON API over the factory's own functions, serving the
built app. read.py turns factory state into the API's shapes, server.py is the HTTP side and its guards, and
fixture.py builds the scratch factory the tests serve."""
