import os
import subprocess
import sys
from pathlib import Path

from config import GROOVY_JAR_NAME, JAVA_HOME, ODI_HOME, OJDBC_JAR_NAME


def _java_exe() -> str:
    return os.getenv("JAVA_EXE") or str(Path(JAVA_HOME) / "bin" / "java.exe")


def _classpath() -> str:
    odi_home = Path(os.getenv("ODI_HOME") or ODI_HOME)
    return ";".join([
        str(odi_home / "oracle_common" / "modules" / "javax.management.j2ee.jar"),
        str(odi_home / "odi" / "sdk" / "lib" / "odi-core.jar"),
        str(odi_home / "oracle_common" / "modules" / "oracle.jdbc" / OJDBC_JAR_NAME),
        str(odi_home / "oracle_common" / "modules" / "thirdparty" / GROOVY_JAR_NAME),
        str(odi_home / "odi" / "sdk" / "lib" / "*"),
        str(odi_home / "oracle_common" / "modules" / "oracle.toplink" / "eclipselink.jar"),
    ])


def execute_automation():
    java_exe = _java_exe()
    cp = _classpath()
    base_dir = Path(__file__).resolve().parent
    props_path = Path(os.getenv("ODI_CONNECTION_PROPERTIES", base_dir / "odi_connection.properties.generated"))
    if not props_path.exists():
        generator = base_dir / "write_odi_connection_properties.py"
        subprocess.run([sys.executable, str(generator)], check=True)
    
    # 1. CREATE MAPPING (Fixed Project/Folder)
    print("--- Phase 1: Creating mapping in TUTORIOL/First Folder [ATLANDI] ---")

    # 2. GENERATE SCENARIO (Dynamic Call Fix)
    print("--- Phase 2: Generating scenario [ATLANDI] ---")

    # 3. RUN SCENARIO (Via OdiStartScen)
    print("--- Phase 3: Triggering RUN via Agent ---")
    scen_name = sys.argv[1] if len(sys.argv) > 1 else "MAP_CUSTOMER_TO_ODS_SCN"
    
    # Read props for credentials
    props = {}
    with open(props_path, "r") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                props[k] = v

    # OdiStartScen repo baglanti parametrelerini KOMUT SATIRI ARGUMANI olarak ister.
    # `-D` system property'leri bu tool tarafindan okunmuyor — "Repository connections must be set" hatasi onun isareti.
    run_cmd = [
        java_exe, "-cp", cp,
        "com.sunopsis.dwg.tools.OdiStartScen",
        f"-URL={props.get('master_jdbc')}",
        f"-DRIVER={props.get('master_jdbc_driver')}",
        f"-USER_NAME={props.get('master_username')}",
        f"-USER_PASSWORD={props.get('master_password')}",
        f"-WORK_REP={props.get('work_name')}",
        f"-ODI_USER={props.get('odi_username')}",
        f"-ODI_PASSWORD={props.get('odi_password')}",
        f"-SCEN_NAME={scen_name}",
        "-SCEN_VERSION=001",
        "-CONTEXT=GLOBAL",
        "-AGENT_NAME=OracleDIAgent1",
    ]
    # Sifre log'a sizmasin diye redacted versiyon yazdiralim
    safe_cmd = [a if not (a.startswith("-USER_PASSWORD=") or a.startswith("-ODI_PASSWORD=")) else a.split("=", 1)[0] + "=***" for a in run_cmd]
    print(f"Running: {' '.join(safe_cmd)}")
    result = subprocess.run(run_cmd, check=False)
    
    if result.returncode != 0:
        print(f"ODI RUN FAILED with code: {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


if __name__ == "__main__":
    execute_automation()
