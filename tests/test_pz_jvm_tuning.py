from ops.pz_jvm_tuning import tune_config


def test_pz_tuning_replaces_zgc_without_touching_gameplay_config():
    original = {
        "mainClass": "zombie/network/GameServer",
        "classpath": ["java/."],
        "vmArgs": [
            "-Djava.awt.headless=true",
            "-Xmx6g",
            "-Dzomboid.steam=1",
            "-XX:+UseZGC",
        ],
        "gameplay": {"zombiePopulation": 1},
    }

    tuned = tune_config(original)

    assert tuned["vmArgs"] == [
        "-Djava.awt.headless=true",
        "-Xms6g",
        "-Xmx6g",
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=50",
        "-XX:+ParallelRefProcEnabled",
        "-XX:+PerfDisableSharedMem",
        "-Dzomboid.steam=1",
    ]
    assert tuned["gameplay"] == original["gameplay"]
    assert tune_config(tuned) == tuned
