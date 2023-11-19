# Copyright 2018-2021 The glTF-Blender-IO authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

import bpy
import sys
import traceback

from ...io.com.gltf2_io_debug import print_console, print_newline
from ...io.exp import gltf2_io_export
from ...io.exp import gltf2_io_draco_compression_extension
from ...io.exp.gltf2_io_user_extensions import export_user_extensions
from ..com import gltf2_blender_json
from . import gltf2_blender_gather
from .gltf2_blender_gltf2_exporter import GlTF2Exporter


def save(context, export_settings):
    """Start the glTF 2.0 export and saves to content either to a .gltf or .glb file."""
    if bpy.context.active_object is not None:
        if bpy.context.active_object.mode != "OBJECT": # For linked object, you can't force OBJECT mode
            bpy.ops.object.mode_set(mode='OBJECT')

    original_frame = bpy.context.scene.frame_current
    if not export_settings['gltf_current_frame']:
        bpy.context.scene.frame_set(0)

    __notify_start(context)
    start_time = time.time()
    pre_export_callbacks = export_settings["pre_export_callbacks"]
    for callback in pre_export_callbacks:
        callback(export_settings)

    json, buffer = __export(export_settings)

    post_export_callbacks = export_settings["post_export_callbacks"]
    for callback in post_export_callbacks:
        callback(export_settings)
    __write_file(json, buffer, export_settings)

    end_time = time.time()
    __notify_end(context, end_time - start_time)

    if not export_settings['gltf_current_frame']:
        bpy.context.scene.frame_set(int(original_frame))
    return {'FINISHED'}


def __export(export_settings):
    exporter = GlTF2Exporter(export_settings)
    __gather_gltf(exporter, export_settings)
    buffer = __create_buffer(exporter, export_settings)
    exporter.finalize_images()

    export_user_extensions('gather_gltf_extensions_hook', export_settings, exporter.glTF)
    exporter.traverse_extensions()

    # Detect extensions that are animated
    # If they are not animated, we can remove the extension if it is empty (all default values), and if default values don't change the shader
    # But if they are animated, we need to keep the extension, even if it is empty
    __detect_animated_extensions(exporter.glTF.to_dict(), export_settings)

    # now that addons possibly add some fields in json, we can fix if needed
    # Also deleting no more needed extensions, based on what we detected above
    json = __fix_json(exporter.glTF.to_dict(), export_settings)

    # IOR is a special case where we need to export only if some other extensions are used
    __check_ior(json, export_settings)

    # Volum is a special case where we need to export only if transmission is used
    __check_volume(json, export_settings)

    __manage_extension_declaration(json, export_settings)


    # We need to run it again, as we can now have some "extensions" dict that are empty
    # Or extensionsUsed / extensionsRequired that are empty
    # (because we removed some extensions)
    json = __fix_json(json, export_settings)

    # Convert additional data if needed
    if export_settings['gltf_unused_textures'] is True:
        additional_json_textures = __fix_json([i.to_dict() for i in exporter.additional_data.additional_textures], export_settings)

        # Now that we have the final json, we can add the additional data
        if len(additional_json_textures) > 0:
            if json.get('extras') is None:
                json['extras'] = {}
            json['extras']['additionalTextures'] = additional_json_textures

    return json, buffer

def __check_ior(json, export_settings):
    if 'materials' not in json.keys():
        return
    for mat in json['materials']:
        if 'extensions' not in mat.keys():
            continue
        if 'KHR_materials_ior' not in mat['extensions'].keys():
            continue
        # We keep IOR only if some other extensions are used
        # And because we may have deleted some extensions, we need to check again
        need_to_export_ior = [
            'KHR_materials_transmission',
            'KHR_materials_volume',
            'KHR_materials_specular'
        ]

        if not any([e in mat['extensions'].keys() for e in need_to_export_ior]):
            del mat['extensions']['KHR_materials_ior']

    # Check if we need to keep the extension declaration
    ior_found = False
    for mat in json['materials']:
        if 'extensions' not in mat.keys():
            continue
        if 'KHR_materials_ior' not in mat['extensions'].keys():
            continue
        ior_found = True
        break
    if not ior_found:
        export_settings['gltf_need_to_keep_extension_declaration'] = [e for e in export_settings['gltf_need_to_keep_extension_declaration'] if e != 'KHR_materials_ior']

def __check_volume(json, export_settings):
    if 'materials' not in json.keys():
        return
    for mat in json['materials']:
        if 'extensions' not in mat.keys():
            continue
        if 'KHR_materials_volume' not in mat['extensions'].keys():
            continue
        # We keep volume only if transmission is used
        # And because we may have deleted some extensions, we need to check again
        if 'KHR_materials_transmission' not in mat['extensions'].keys():
            del mat['extensions']['KHR_materials_volume']

    # Check if we need to keep the extension declaration
    volume_found = False
    for mat in json['materials']:
        if 'extensions' not in mat.keys():
            continue
        if 'KHR_materials_volume' not in mat['extensions'].keys():
            continue
        volume_found = True
        break
    if not volume_found:
        export_settings['gltf_need_to_keep_extension_declaration'] = [e for e in export_settings['gltf_need_to_keep_extension_declaration'] if e != 'KHR_materials_volume']


def __detect_animated_extensions(obj, export_settings):
    export_settings['gltf_animated_extensions'] = []
    export_settings['gltf_need_to_keep_extension_declaration'] = []
    if not 'animations' in obj.keys():
        return
    for anim in obj['animations']:
        if 'extensions' in anim.keys():
            for channel in anim['channels']:
                if not channel['target']['path'] == "pointer":
                    continue
                pointer = channel['target']['extensions']['KHR_animation_pointer']['pointer']
                if not "/KHR" in pointer:
                    continue
                tab = pointer.split("/")
                tab = [i for i in tab if i.startswith("KHR_")]
                if len(tab) == 0:
                    continue
                if tab[-1] not in export_settings['gltf_animated_extensions']:
                    export_settings['gltf_animated_extensions'].append(tab[-1])

def __manage_extension_declaration(json, export_settings):
    if 'extensionsUsed' in json.keys():
        new_ext_used = []
        for ext in json['extensionsUsed']:
            if ext not in export_settings['gltf_need_to_keep_extension_declaration']:
                continue
            new_ext_used.append(ext)
        json['extensionsUsed'] = new_ext_used
    if 'extensionsRequired' in json.keys():
        new_ext_required = []
        for ext in json['extensionsRequired']:
            if ext not in export_settings['gltf_need_to_keep_extension_declaration']:
                continue
            new_ext_required.append(ext)
        json['extensionsRequired'] = new_ext_required

def __gather_gltf(exporter, export_settings):
    active_scene_idx, scenes, animations = gltf2_blender_gather.gather_gltf2(export_settings)

    unused_skins = export_settings['vtree'].get_unused_skins()

    if export_settings['gltf_draco_mesh_compression']:
        gltf2_io_draco_compression_extension.encode_scene_primitives(scenes, export_settings)
        exporter.add_draco_extension()

    export_user_extensions('gather_gltf_hook', export_settings, active_scene_idx, scenes, animations)

    for idx, scene in enumerate(scenes):
        exporter.add_scene(scene, idx==active_scene_idx, export_settings=export_settings)
    for animation in animations:
        exporter.add_animation(animation)
    exporter.traverse_unused_skins(unused_skins)
    exporter.traverse_additional_textures()
    exporter.traverse_additional_images()


def __create_buffer(exporter, export_settings):
    buffer = bytes()
    if export_settings['gltf_format'] == 'GLB':
        buffer = exporter.finalize_buffer(export_settings['gltf_filedirectory'], is_glb=True)
    else:
        exporter.finalize_buffer(export_settings['gltf_filedirectory'],
                                 export_settings['gltf_binaryfilename'])

    return buffer


def __fix_json(obj, export_settings):
    # TODO: move to custom JSON encoder
    fixed = obj
    if isinstance(obj, dict):
        fixed = {}
        for key, value in obj.items():
            if key == 'extras' and value is not None:
                fixed[key] = value
                continue
            if not __should_include_json_value(key, value, export_settings):
                continue
            fixed[key] = __fix_json(value, export_settings)
    elif isinstance(obj, list):
        fixed = []
        for value in obj:
            fixed.append(__fix_json(value, export_settings))
    elif isinstance(obj, float):
        # force floats to int, if they are integers (prevent INTEGER_WRITTEN_AS_FLOAT validator warnings)
        if int(obj) == obj:
            return int(obj)
    return fixed


def __should_include_json_value(key, value, export_settings):
    allowed_empty_collections = ["KHR_materials_unlit"]
    allowed_empty_collections_if_animated = \
        [
         "KHR_materials_specular",
         "KHR_materials_clearcoat",
         "KHR_texture_transform",
         "KHR_materials_emissive_strength",
         "KHR_materials_ior",
         #"KHR_materials_iridescence",
         "KHR_materials_sheen",
         "KHR_materials_specular",
         "KHR_materials_transmission",
         "KHR_materials_volume",
         "KHR_lights_punctual"
         ]

    if value is None:
        return False
    elif __is_empty_collection(value) and key not in allowed_empty_collections:
        # Empty collection is not allowed, except if it is animated
        if key in allowed_empty_collections_if_animated:
            if key in export_settings['gltf_animated_extensions']:
                # There is an animation, so we can keep this empty collection, and store that this extension declaration needs to be kept
                export_settings['gltf_need_to_keep_extension_declaration'].append(key)
                return True
            else:
                # There is no animation, so we will not keep this empty collection
                return False
        # We can't have this empty collection, because it can't be animated
        return False
    elif not __is_empty_collection(value):
        if key.startswith("KHR_") or key.startswith("EXT_"):
            export_settings['gltf_need_to_keep_extension_declaration'].append(key)
    elif __is_empty_collection(value) and key in allowed_empty_collections:
        # We can have this empty collection for this extension. So keeping it, and store that this extension declaration needs to be kept
        export_settings['gltf_need_to_keep_extension_declaration'].append(key)
    return True


def __is_empty_collection(value):
    return (isinstance(value, dict) or isinstance(value, list)) and len(value) == 0


def __write_file(json, buffer, export_settings):
    try:
        gltf2_io_export.save_gltf(
            json,
            export_settings,
            gltf2_blender_json.BlenderJSONEncoder,
            buffer)
    except AssertionError as e:
        _, _, tb = sys.exc_info()
        traceback.print_tb(tb)  # Fixed format
        tb_info = traceback.extract_tb(tb)
        for tbi in tb_info:
            filename, line, func, text = tbi
            print_console('ERROR', 'An error occurred on line {} in statement {}'.format(line, text))
        print_console('ERROR', str(e))
        raise e


def __notify_start(context):
    print_console('INFO', 'Starting glTF 2.0 export')
    context.window_manager.progress_begin(0, 100)
    context.window_manager.progress_update(0)


def __notify_end(context, elapsed):
    print_console('INFO', 'Finished glTF 2.0 export in {} s'.format(elapsed))
    context.window_manager.progress_end()
    print_newline()
