# ODI-87 public SDK probe contracts

The fixed read-only mapping probe enumerates `MapPhysicalDesign.getAllAPNodes()`
and reads `MapPhysicalNode.getLKMName()`. An extract KM (`getXKMName`) is not the
loading KM and cannot certify the access point. Every access point must use the
exact reviewed built-in LKM. Zero access points is valid for same-location loads.
The probe also requires the exact SCD IKM and all 26 target attributes mapped.
Count fields accepted by the Python normalizer must be integers, not booleans or
floating-point values that compare equal to the expected count.

The generic writer reads options through public `IOptionValue.getOptionValue()`
after setting them. Missing or sticky flow/static-control options block creation.
The SDK does not expose the previously called `getValue()` method.

Tests execute the real read-only script against isolated fake SDK classes, and
the real generic option-check block against an option exposing only the public
accessor. Existing SDK signature checks and script compilation remain separate.
This is synthetic/offline evidence, not live mapping, scenario or load proof.
No repository or database connection is opened by these tests. The remaining
audit-role repair, scenario ownership/version cutover and API postflight wiring
are separate code-first packets; this document does not claim them complete.
