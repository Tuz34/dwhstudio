-- Retired duplicate DDL entrypoint.
-- Review and run ddl/audit_schema_contract.sql explicitly after approval.
BEGIN
    RAISE_APPLICATION_ERROR(-20090, 'Retired DDL; use ddl/audit_schema_contract.sql after review and approval.');
END;
/
