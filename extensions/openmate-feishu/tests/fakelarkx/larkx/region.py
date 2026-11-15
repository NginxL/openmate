from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    name: str
    label: str
    domain: str
    locale: str

    @property
    def file_host(self):
        return "https://files.%s" % self.domain


REGIONS = {"feishu": Region("feishu", "飞书", "feishu.example", "zh_CN"), "lark": Region("lark", "Lark", "lark.example", "en_US")}


def get_region(name):
    r = REGIONS.get((name or "").strip().lower())
    if r is None:
        raise ValueError("unknown region")
    return r
