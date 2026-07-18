0.0.7 (2026-07-18)
------------------

- replace pygame w/ pygame-ce


0.0.6 (2026-04-01)

- configure pulp solver to keep stdout quiet
- change global vars to upper case
- add ci/cd workflow


## 0.0.5 (2025-06-04)

- restore floating window to focus when returning to WS
- restore window focus when expo is closed/toggled
- track previously focused window class in global_knowldege to add
  extra precaution for not capturing blacklisted windows


## 0.0.4 (2025-03-15)

- lock process to guarantee single instance
- store screenshot with its dimensions, so it's never ambiguous on usage


## 0.0.3 (2025-03-10)

- persist and restore state on i3 restarts


## 0.0.2 (2025-02-05)

- fix output_blacklist config logic
- add screenshot_lib_path config item


## 0.0.1 (2024-10-28)

- add license (MIT)
- configure drone.ci to publish releases to pypi
