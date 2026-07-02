from typing import Optional, TypedDict, Union


class SettingLedgerEntry(TypedDict):
    type: str
    namespace: str
    key: str
    prior_value: Optional[str]
    new_value: Optional[str]

class DeviceConfigLedgerEntry(TypedDict):
    type: str
    namespace: str
    key: str
    prior_value: Optional[str]
    new_value: Optional[str]

class AppOpLedgerEntry(TypedDict):
    type: str
    package: str
    op: str
    prior_value: Optional[str]
    new_value: Optional[str]

class StandbyBucketLedgerEntry(TypedDict):
    type: str
    package: str
    prior_value: Optional[str]
    new_value: Optional[str]

class PackageEnabledLedgerEntry(TypedDict):
    type: str
    package: str
    prior_value: Optional[bool]
    new_value: Optional[bool]

class NetpolicyRestrictBackgroundEntry(TypedDict):
    type: str
    prior_value: bool
    new_value: bool

class NetpolicyWhitelistEntry(TypedDict):
    type: str
    package: str
    uid: str
    prior_member: bool
    new_value: bool

class DeviceIdleWhitelistEntry(TypedDict):
    type: str
    package: str
    prior_member: bool
    new_value: bool

class AppHibernationEntry(TypedDict):
    type: str
    package: str
    prior_value: bool
    new_value: bool

AnyLedgerEntry = Union[
    SettingLedgerEntry,
    DeviceConfigLedgerEntry,
    AppOpLedgerEntry,
    StandbyBucketLedgerEntry,
    PackageEnabledLedgerEntry,
    NetpolicyRestrictBackgroundEntry,
    NetpolicyWhitelistEntry,
    DeviceIdleWhitelistEntry,
    AppHibernationEntry,
]
