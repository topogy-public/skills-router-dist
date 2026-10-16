# Vendored third-party code

`yaml/` is the pure-Python PyYAML package (v6.0.3, MIT-licensed, see
`yaml/LICENSE`), vendored so the router's hooks run against a stock `python3`
with no `pip install`. Only the `.py` modules are included — the optional C
extension (`_yaml`) is omitted, so PyYAML uses its pure-Python loader. `lib.py`
prefers a system PyYAML when one is installed and falls back to this copy.
