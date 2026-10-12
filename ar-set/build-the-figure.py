# The demo AR set: a Triadic Ballet figure on the anchor sheet.
#
# Oskar Schlemmer dressed dancers as geometry — spheres, cones, discs,
# cylinders — in the primaries plus black, white and gold. The figure here is
# built from those solids rather than from a human silhouette, which is both
# closer to the source and far easier to author procedurally than a body.
#
# The app's contract: real metres, Blender's own Z-up, the anchor lying flat
# at the origin, the set facing -Y (towards the viewer).
import bpy, math, sys
from mathutils import Vector

OUT = sys.argv[sys.argv.index('--') + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
scene.unit_settings.length_unit = 'METERS'

# Schlemmer's palette: the three primaries, with black, white and a brass gold.
PAL = {
    'red'   : (0.78, 0.12, 0.09, 1),
    'yellow': (0.95, 0.75, 0.08, 1),
    'blue'  : (0.09, 0.24, 0.58, 1),
    'white' : (0.93, 0.92, 0.88, 1),
    'black' : (0.07, 0.07, 0.08, 1),
    'gold'  : (0.80, 0.62, 0.22, 1),
}
_mats = {}
def mat(name):
    if name in _mats: return _mats[name]
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes['Principled BSDF']
    b.inputs['Base Color'].default_value = PAL[name]
    b.inputs['Roughness'].default_value = 0.35 if name == 'gold' else 0.6
    if name == 'gold': b.inputs['Metallic'].default_value = 0.9
    _mats[name] = m
    return m

def solid(kind, name, loc, size, colour, rot=(0,0,0)):
    if kind == 'sphere':
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=28, ring_count=14, location=loc)
    elif kind == 'cone':
        bpy.ops.mesh.primitive_cone_add(radius1=0.5, radius2=0.0, depth=1.0, vertices=28, location=loc)
    elif kind == 'cylinder':
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0, vertices=28, location=loc)
    elif kind == 'disc':
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0, vertices=36, location=loc)
    o = bpy.context.object
    o.name = name
    o.scale = Vector(size)
    o.rotation_euler = rot
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat(colour))
    return o

# ------------------------------------------------------------------ the stage
# A black disc: the dancer's floor, and it reads as deliberate rather than as
# a dropped shadow.
solid('disc', 'plinth', (0, 0, 0.003), (0.17, 0.17, 0.006), 'black')

# Two upright surfaces for video. The names are the contract — the app binds
# a film to each by name.
def screen(name, x, yaw):
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=(x, 0.058, 0.085))
    o = bpy.context.object
    o.name = name
    o.scale = (0.095, 0.053, 1.0)
    o.rotation_euler = (math.radians(90), 0, math.radians(yaw))
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.001)
    bpy.ops.object.mode_set(mode='OBJECT')
    o['video'] = name + '.mp4'            # the binding travels inside the model
    o.data.materials.append(mat('white'))
    return o
screen('screen-left',  -0.056, 16)
screen('screen-right',  0.056, -16)

# ------------------------------------------------------------------ the figure
# Read from the floor up: cone skirt, spherical hips, cylinder torso, ruff
# disc, ball head, brass cone hat. Arms are cylinders with ball joints.
H = 0.21
parts = [
    solid('cone',     'skirt',    (0, 0, 0.038), (0.088, 0.088, 0.076), 'red',    (math.radians(0),0,0)),
    solid('sphere',   'hips',     (0, 0, 0.082), (0.052, 0.046, 0.046), 'yellow'),
    solid('cylinder', 'torso',    (0, 0, 0.116), (0.050, 0.042, 0.052), 'blue'),
    solid('disc',     'ruff',     (0, 0, 0.144), (0.072, 0.072, 0.008), 'white'),
    solid('sphere',   'head',     (0, 0, 0.168), (0.040, 0.038, 0.042), 'white'),
    solid('cone',     'hat',      (0, 0, 0.196), (0.036, 0.036, 0.034), 'gold'),
    solid('sphere',   'joint_L',  ( 0.034, 0, 0.134), (0.020, 0.020, 0.020), 'gold'),
    solid('sphere',   'joint_R',  (-0.034, 0, 0.134), (0.020, 0.020, 0.020), 'gold'),
    solid('cylinder', 'arm_L',    ( 0.034, 0, 0.108), (0.015, 0.015, 0.048), 'yellow'),
    solid('cylinder', 'arm_R',    (-0.034, 0, 0.108), (0.015, 0.015, 0.048), 'yellow'),
    solid('cylinder', 'leg_L',    ( 0.016, 0, 0.022), (0.017, 0.017, 0.044), 'black'),
    solid('cylinder', 'leg_R',    (-0.016, 0, 0.022), (0.017, 0.017, 0.044), 'black'),
]
bpy.ops.object.select_all(action='DESELECT')
for o in parts: o.select_set(True)
bpy.context.view_layer.objects.active = parts[0]
bpy.ops.object.join()
body = bpy.context.object
body.name = 'figure'

# ---------------------------------------------------------------- the armature
bpy.ops.object.armature_add(location=(0, 0, 0))
arm = bpy.context.object
arm.name = 'rig'
bpy.ops.object.mode_set(mode='EDIT')
eb = arm.data.edit_bones
for b in list(eb): eb.remove(b)

def bone(name, head, tail, parent=None, connect=False):
    b = eb.new(name)
    b.head, b.tail = Vector(head), Vector(tail)
    if parent: b.parent, b.use_connect = eb[parent], connect
    return b

# Every animated bone points the same way as its parent, so its rest rotation
# is identity. Limbs pointing down from parents pointing up would sit at
# exactly 180 degrees — the quaternion antipode — and a swing about it makes
# the exporter flip sign between keys, which glTF's linear interpolation then
# takes the long way round.
bone('root',        (0, 0, 0.100), (0, 0, 0.060))
bone('spine',       (0, 0, 0.100), (0, 0, 0.150), 'root')
bone('head',        (0, 0, 0.150), (0, 0, 0.200), 'spine', True)
for s, x in (('L', 1), ('R', -1)):
    bone(f'shoulder.{s}', ( 0.034*x, 0, 0.140), ( 0.034*x, 0, 0.132), 'spine')
    bone(f'arm.{s}',      ( 0.034*x, 0, 0.132), ( 0.034*x, 0, 0.086), f'shoulder.{s}', True)
    bone(f'hip.{s}',      ( 0.016*x, 0, 0.060), ( 0.016*x, 0, 0.044), 'root', False)
    bone(f'leg.{s}',      ( 0.016*x, 0, 0.044), ( 0.016*x, 0, 0.000), f'hip.{s}', True)
bpy.ops.object.mode_set(mode='OBJECT')

bpy.ops.object.select_all(action='DESELECT')
body.select_set(True); arm.select_set(True)
bpy.context.view_layer.objects.active = arm
try:
    bpy.ops.object.parent_set(type='ARMATURE_AUTO'); print('skinned: automatic weights')
except RuntimeError as e:
    print('automatic weights failed (%s); envelopes instead' % e)
    bpy.ops.object.parent_set(type='ARMATURE_ENVELOPE')

# ------------------------------------------------------------- the slow pavane
# Schlemmer's dancers do not stride; they turn and step with a mechanical
# evenness. A slow rotation of the whole figure with a small step and sway
# reads far more like the Triadic Ballet than a walk cycle does.
scene.frame_start, scene.frame_end = 0, 96
bpy.ops.object.mode_set(mode='POSE')
pb = arm.pose.bones
for f in range(0, 97, 4):
    t = (f / 96.0) * 2 * math.pi
    r = pb['root']; r.rotation_mode = 'XYZ'
    # A sway, not a revolution. A full turn passes through 180 degrees, where
    # the exporter canonicalises the quaternion to w >= 0 and so flips its
    # sign; glTF interpolates linearly between those two keys and the figure
    # whips round once per turn. Plus or minus 45 degrees keeps the half-angle
    # under 23 and never goes near it.
    r.rotation_euler.z = math.sin(t) * math.radians(45)
    r.location.z = abs(math.sin(t * 2)) * 0.004        # the step
    r.keyframe_insert('rotation_euler', frame=f)
    r.keyframe_insert('location', frame=f)
    for s, sign in (('L', 1), ('R', -1)):
        a = pb[f'arm.{s}']; a.rotation_mode = 'XYZ'
        a.rotation_euler.x = math.sin(t + (0 if sign > 0 else math.pi)) * math.radians(20)
        a.keyframe_insert('rotation_euler', frame=f)
        l = pb[f'leg.{s}']; l.rotation_mode = 'XYZ'
        l.rotation_euler.x = math.sin(t * 2 + (0 if sign > 0 else math.pi)) * math.radians(12)
        l.keyframe_insert('rotation_euler', frame=f)
bpy.ops.object.mode_set(mode='OBJECT')

def all_fcurves(action):
    if hasattr(action, 'fcurves'): return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for cbag in getattr(strip, 'channelbags', []): out.extend(cbag.fcurves)
    return out
action = arm.animation_data.action
curves = all_fcurves(action)
print('fcurves found:', len(curves))
for fc in curves:
    for kp in fc.keyframe_points: kp.interpolation = 'LINEAR'
action.name = 'pavane'

bpy.ops.wm.save_as_mainfile(filepath=OUT.replace('.glb', '.blend'))
bpy.ops.export_scene.gltf(filepath=OUT, export_format='GLB', export_yup=False,
                          export_extras=True, export_animations=True,
                          export_skins=True, export_apply=True)
print('EXPORTED', OUT)
