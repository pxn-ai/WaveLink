class stream_args:
    def __init__(self, cpu_format="fc32", args="", channels=(0,)): self.cpu_format=cpu_format
class usrp_source:
    def __init__(self, dev_args, stream_args): assert "type=ant" in dev_args
    def __getattr__(self, n): return lambda *a, **k: None
class usrp_sink(usrp_source):
    def __init__(self, dev_args, stream_args, len_tag=""): assert "type=ant" in dev_args
