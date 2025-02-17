import taichi as ti
import math
import numpy as np
import os

ti.init(arch=ti.gpu)
vec = ti.math.vec2

SAVE_FRAMES = False

window_size = 1024 # Number of pixels of the window
max_n = 1024 * 16  # Maximum number of grains

density = 100.0
stiffness = 8
restitution_coef = 0.001
gravity = -9.81
dt = 0.0001  # Larger dt might lead to unstable results.
substeps = 60


@ti.dataclass
class Grain:
    p: vec  # Position
    m: ti.f32  # Mass
    r: ti.f32  # Radius
    v: vec  # Velocity
    a: vec  # Acceleration
    f: vec  # Force
    c: ti.u32  # Color


gf = Grain.field(shape=(max_n, ))
num_grains = ti.field(dtype=ti.i32, shape=())

grid_n = 64
grid_size = 1.0 / grid_n  # Simulation domain of size [0, 1]
print(f"Grid size: {grid_n}x{grid_n}")

grain_r_min = 0.003
grain_r_max = 0.004

assert grain_r_max * 2 < grid_size

collider_radius = 0.05
num_colliders = 9
colliders = ti.Vector.field(2, dtype=ti.f32, shape=num_colliders)

for i in range(4):
    colliders[i] = [0.2 + 0.2 * i, 0.42]

for i in range(4, 9):
    colliders[i] = [0.1 + 0.2 * (i - 4), 0.27]


@ti.kernel
def init(img: ti.types.ndarray()):
    sample_res = 128
    for x in range(sample_res):
        for y in range(sample_res // 2, sample_res):
            # Spread grains in a restricted area.
            p, q = x * 8 + 4, y * 8 + 4
            i = ti.atomic_add(num_grains[None], 1)
            gf[i].p = vec(p / 1024, q / 1024)
            gf[i].r = ti.random() * (grain_r_max - grain_r_min) + grain_r_min
            gf[i].m = density * math.pi * gf[i].r**2
            gf[i].c = img[p, q, 0] * 65536 + img[p, q, 1] * 256 + img[p, q, 2]
    print(num_grains[None])


@ti.kernel
def update():
    for i in gf:
        a = gf[i].f / gf[i].m
        gf[i].v += (gf[i].a + a) * dt / 2.0
        gf[i].p += gf[i].v * dt + 0.5 * a * dt**2
        gf[i].a = a


@ti.kernel
def apply_bc():
    bounce_coef = 0.3  # Velocity damping
    for i in gf:
        x = gf[i].p[0]
        y = gf[i].p[1]

        if y - gf[i].r < 0:
            gf[i].p[1] = gf[i].r
            gf[i].v[1] *= -bounce_coef

        elif y + gf[i].r > 1.0:
            gf[i].p[1] = 1.0 - gf[i].r
            gf[i].v[1] *= -bounce_coef

        if x - gf[i].r < 0:
            gf[i].p[0] = gf[i].r
            gf[i].v[0] *= -bounce_coef

        elif x + gf[i].r > 1.0:
            gf[i].p[0] = 1.0 - gf[i].r
            gf[i].v[0] *= -bounce_coef

        for j in range(num_colliders):
            delta = (gf[i].p - colliders[j]).norm() - (gf[i].r +
                                                       collider_radius)
            if delta < 0:
                normal = (gf[i].p - colliders[j]).normalized()
                gf[i].v -= normal * min(normal.dot(gf[i].v), 0)
                gf[i].p -= delta * normal


@ti.func
def resolve(i, j):
    rel_pos = gf[j].p - gf[i].p
    dist = ti.sqrt(rel_pos[0]**2 + rel_pos[1]**2)
    delta = -dist + gf[i].r + gf[j].r  # delta = d - 2 * r
    if delta > 0:  # in contact
        normal = rel_pos / dist
        f1 = normal * delta * stiffness * 1000
        # Damping force
        M = (gf[i].m * gf[j].m) / (gf[i].m + gf[j].m)
        K = stiffness
        C = 2. * (1. / ti.sqrt(1. + (math.pi / ti.log(restitution_coef))**2)
                  ) * ti.sqrt(K * M)
        V = (gf[j].v - gf[i].v) * normal
        f2 = C * V * normal
        gf[i].f += f2 - f1
        gf[j].f -= f2 - f1


list_head = ti.field(dtype=ti.i32, shape=grid_n * grid_n)
list_cur = ti.field(dtype=ti.i32, shape=grid_n * grid_n)
list_tail = ti.field(dtype=ti.i32, shape=grid_n * grid_n)

grain_count = ti.field(dtype=ti.i32,
                       shape=(grid_n, grid_n),
                       name="grain_count")
column_sum = ti.field(dtype=ti.i32, shape=grid_n, name="column_sum")
prefix_sum = ti.field(dtype=ti.i32, shape=(grid_n, grid_n), name="prefix_sum")
particle_id = ti.field(dtype=ti.i32, shape=max_n, name="particle_id")


@ti.kernel
def contact(gf: ti.template()):
    '''
    Handle the collision between grains.
    '''
    for i in range(num_grains[None]):
        gf[i].f = vec(0., gravity * gf[i].m)  # Apply gravity.

    grain_count.fill(0)

    for i in range(num_grains[None]):
        grid_idx = ti.floor(gf[i].p * grid_n, int)
        grain_count[grid_idx] += 1

    for i in range(grid_n):
        sum = 0
        for j in range(grid_n):
            sum += grain_count[i, j]
        column_sum[i] = sum

    prefix_sum[0, 0] = 0

    ti.loop_config(serialize=True)
    for i in range(1, grid_n):
        prefix_sum[i, 0] = prefix_sum[i - 1, 0] + column_sum[i - 1]

    for i in range(grid_n):
        for j in range(grid_n):
            if j == 0:
                prefix_sum[i, j] += grain_count[i, j]
            else:
                prefix_sum[i, j] = prefix_sum[i, j - 1] + grain_count[i, j]

            linear_idx = i * grid_n + j

            list_head[linear_idx] = prefix_sum[i, j] - grain_count[i, j]
            list_cur[linear_idx] = list_head[linear_idx]
            list_tail[linear_idx] = prefix_sum[i, j]

    for i in range(num_grains[None]):
        grid_idx = ti.floor(gf[i].p * grid_n, int)
        linear_idx = grid_idx[0] * grid_n + grid_idx[1]
        grain_location = ti.atomic_add(list_cur[linear_idx], 1)
        particle_id[grain_location] = i

    # Brute-force collision detection
    '''
    for i in range(n):
        for j in range(i + 1, n):
            resolve(i, j)
    '''

    # Fast collision detection
    for i in range(num_grains[None]):
        grid_idx = ti.floor(gf[i].p * grid_n, int)
        x_begin = max(grid_idx[0] - 1, 0)
        x_end = min(grid_idx[0] + 2, grid_n)

        y_begin = max(grid_idx[1] - 1, 0)
        y_end = min(grid_idx[1] + 2, grid_n)

        for neigh_i in range(x_begin, x_end):
            for neigh_j in range(y_begin, y_end):
                neigh_linear_idx = neigh_i * grid_n + neigh_j
                for p_idx in range(list_head[neigh_linear_idx],
                                   list_tail[neigh_linear_idx]):
                    j = particle_id[p_idx]
                    if i < j:
                        resolve(i, j)


source = ti.tools.imread('source.png')
init(source)
gui = ti.GUI('Taichi DEM', (window_size, window_size),
             background_color=0x222222)
step = 0

if SAVE_FRAMES:
    os.makedirs('output', exist_ok=True)

while gui.running:
    for s in range(substeps):
        update()
        apply_bc()
        contact(gf)
    pos = gf.p.to_numpy()
    r = gf.r.to_numpy() * window_size
    gui.circles(pos, radius=r, color=gf.c.to_numpy())
    gui.circles(colliders.to_numpy(),
                radius=collider_radius * window_size,
                color=0xffffff)
    if SAVE_FRAMES:
        gui.show(f'output/{step:06d}.png')
    else:
        gui.show()
    step += 1
