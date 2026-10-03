from src.open_llm_vtuber.room import minecraft_projects as mp


def test_offline_owner_id_is_a_four_int_array():
    text = mp.offline_uuid_ints("Mika")
    assert text.startswith("[I;") and text.count(",") == 3


def test_build_commands_get_absolute_numbers():
    out = mp.place("fill {x-9} {y4} {z9} {x9} {y} {z-9} minecraft:stone hollow", (100, 64, -20), {})
    assert out == "fill 91 68 -11 109 64 -29 minecraft:stone hollow"
    nbt = mp.place('summon minecraft:wolf {x0} {y1} {z0} {Owner:%MIKA%,PersistenceRequired:1b}', (0, 64, 0), {"Mika": "[I;1,2,3,4]"})
    assert nbt == "summon minecraft:wolf 0 65 0 {Owner:[I;1,2,3,4],PersistenceRequired:1b}"


def test_every_project_has_buildable_milestones():
    for project in mp.PROJECTS:
        for name, gather, build in project["milestones"]:
            commands = build()
            assert name and gather and commands
            assert all("{" not in mp.place(c, (0, 64, 0), {"Mika": "[I;0,0,0,0]", "Luna": "[I;0,0,0,0]"}).split(" {")[0] for c in commands)
