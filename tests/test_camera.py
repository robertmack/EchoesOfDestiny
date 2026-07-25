from remembering.camera import Camera


def test_camera_transforms_world_and_screen_coordinates() -> None:
    camera = Camera(190, 46, 690, 674, x=100, y=200)

    assert camera.world_to_screen((125, 230)) == (215, 76)
    assert camera.screen_to_world((215, 76)) == (125, 230)
    assert camera.screen_to_world((10, 76)) is None


def test_camera_follows_only_after_player_leaves_dead_zone() -> None:
    camera = Camera(190, 46, 690, 674)

    camera.follow((300, 300), (2070, 2022))
    assert (camera.x, camera.y) == (0.0, 0.0)

    camera.follow((650, 630), (2070, 2022))
    assert camera.x > 0
    assert camera.y > 0


def test_camera_clamps_to_large_map_edges() -> None:
    camera = Camera(190, 46, 690, 674, x=10_000, y=10_000)

    camera.clamp((2070, 2022))

    assert camera.x == 1380
    assert camera.y == 1348


def test_zoom_keeps_world_point_under_cursor() -> None:
    camera = Camera(190, 46, 690, 674, x=100, y=200)
    cursor = (500, 350)
    before = camera.screen_to_world(cursor)

    camera.set_zoom(1.5, cursor, (2070, 2022))

    assert camera.screen_to_world(cursor) == before
    assert camera.zoom == 1.5


def test_screen_drag_pans_camera_at_current_zoom() -> None:
    camera = Camera(190, 46, 690, 674, x=500, y=500, zoom=2.0)

    camera.pan_by_screen((40, -20), (2070, 2022))

    assert camera.x == 480
    assert camera.y == 510
