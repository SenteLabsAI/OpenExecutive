"""The Executive in the person's own web browser, through the Open Executive
browser extension (``packages/extension``).

- ``relay``: the steps waiting for each person's extension, and its answers;
- ``tools``: the browser tools the Executive calls (registered as a built-in
  extension, ``orchestrator.extensions.BUILTIN``);
- ``handover``: a step only the person should do ("Waiting on you");
- ``starters``: the "Try one" asks chat suggests once it's connected.

The extension talks to ``api/routes/browser.py`` through the UI's backend
proxy, signed in as that person. ``OE_BROWSER_OFF=1`` turns all of it off.
"""
