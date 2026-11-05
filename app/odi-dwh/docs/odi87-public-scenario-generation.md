# Public scenario generation and ownership

Scenario generation uses the installed public `IOdiScenarioGenerator` interface
with `OdiScenarioGeneratorImpl`, passing the persisted mapping as its source.
It no longer constructs internal Sunopsis deployment bridges. The public SDK
participates in the surrounding ODI transaction. Both the returned scenario and
an exact finder read-back must match the requested name, version and mapping
source before commit. Error/fatal generation issues block; issue details and
repository IDs are never emitted. Ownership is never manufactured by calling
`setSourceMappingId`.

An existing exact version is reused only with matching ownership. Unowned or
foreign versions remain blocked and unchanged. Other names cannot count as the
requested scenario, and an ambiguous exact version or unknown finder result
cannot establish absence. There is no delete, regeneration, run or automatic
latest-version fallback in this creator. Fixed DIM cutover version selection is
a separate reviewed integration, not authorization to replace version 001.

Commit attempts and acknowledgements are distinct. A failed acknowledgement or
rollback yields unknown side effects and requires manual review. A lost report
after acknowledged commit remains committed-but-unverified, not no-effect.
Closing a standalone SDK instance cannot overwrite the emitted outcome. A
transport failure before any JSON reaches the caller still needs conservative
handling by the separate orchestrator integration packet.

Offline tests execute the full real script on isolated fake SDK objects and
separately check public signatures/imports against the installed jar without
creating an instance. They do not prove native ODI generation, Oracle behavior
or live ownership. Later acceptance must verify those properties and actual
stock checkbox encodings without printing expressions, task text or IDs.

Public API reference:
https://docs.oracle.com/middleware/122126/odi/reference-java-api/oracle/odi/generation/IOdiScenarioGenerator.html
