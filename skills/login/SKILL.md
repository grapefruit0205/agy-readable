---
name: login
description: Sign agy (the Antigravity CLI) in so agy-readable can rewrite answers. Use when agy-readable says Antigravity sign-in is needed, or the user asks to sign agy or Antigravity in for agy-readable, or runs /agy-readable:login.
---

Run `agy-readable login` with the Bash tool (it is on the PATH while this plugin is enabled), with no arguments. It starts agy's own Google sign-in, opens the sign-in page in the user's browser when there is one, and prints what to do.

Then tell the user, in their language, what it printed, including the sign-in URL exactly as printed if there is one: sign in on that page, copy the code the page shows, and paste the code by itself into Claude Code's input box within 60 seconds. agy-readable takes the code from the input box and hands it to agy; it never reaches you.

Never ask the user to send you the code any other way, and never pass a code to agy or to any command yourself. agy stores its own sign-in; agy-readable never reads or keeps credentials.
