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


def test_creative_pieces_build_the_same_thing_layer_by_layer():
    from src.open_llm_vtuber.room import minecraft_projects as mp

    steps = mp.split_steps(mp._castle_walls(), "castle")
    commands = [c for s in steps for c in s["commands"]]
    # the hollow wall box comes as 5 layers: full floor, rings, full top
    layers = [c for c in commands if "stone_bricks" in c]
    assert len(layers) == 5 and layers[0].endswith("stone_bricks") and layers[1].endswith("outline")
    assert all("{" in c and "}" in c for c in commands)
    for step in steps:
        fx, _fy, fz = step["focus"]
        vx, vy, vz = step["view"]
        assert ((vx - fx) ** 2 + (vz - fz) ** 2) ** 0.5 >= 6  # she hovers back far enough to see it
    net = mp.split_steps(mp._net_input(), "neural_net")
    assert all(s["view"][0] < 40 for s in net)  # always in front of the network wall
    assert getattr(mp._net_training, "timed", False)
