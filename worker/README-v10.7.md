# v10.7 Mobile keyboard focus fix

- Prevents the remote screenshot image from stealing focus on finger release.
- Removes `tabindex` from the screenshot and disables drag/touch default behavior.
- Focuses the hidden textarea synchronously during the user gesture.
- Attempts to preserve textarea focus if a mobile browser briefly blurs it during screenshot refresh.

Deploy only the worker folder to Railway. Vercel changes are not required.
