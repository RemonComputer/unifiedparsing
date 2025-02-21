
import os
import re
import warnings

import numpy
from scipy.io import loadmat

from .loadseg import AbstractSegmentation
from .adeseg import meta_folder_path

class PascalSegmentation(AbstractSegmentation):
    """
    Implements AbstractSegmentation for the pascal PARTS dataset.
    """

    def __init__(self, directory, collapse_adjectives=None, version='VOC2010'):
        directory = os.path.expanduser(directory)
        # Default to the latest version present in the directory
        if version is None:
            contents = os.listdir(directory)
            if not list(c for c in contents if re.match('^JPEGImages$', c)):
                version = sorted(c for c in contents if c.startswith('VOC') and
                                 os.path.isdir(os.path.join(directory, c)))[-1]
            else:
                version = ''
        self.directory = directory
        self.version = version
        # Default to using the 'part' directory for part annotations
        self.partdir = 'part'
        if os.path.isfile(os.path.join(directory, 'part2ind.m')):
            self.partdir = ''
        # Default to using the 'part' directory for part annotations
        self.contextdir = 'context'
        # Default to removing left/right/front/back adjectives
        self.collapse_adjectives = collapse_adjectives
        # Load the parts coding metadata from part2ind.m
        codes = load_part2ind(
            os.path.join(meta_folder_path, 'pascal/', 'part2ind.m'))
        # Normalized names
        self.codes = normalize_all_readable(codes, collapse_adjectives)
        self.part_object_names, self.part_names, self.part_key = normalize_part_key(self.codes)
        # Load the PASCAL context segmentation labels 
        self.object_names = load_context_labels(
            os.path.join(meta_folder_path, 'pascal/', 'context_labels.txt'))
        self.unknown_label = self.object_names.index('unknown')
        self.object_names[self.unknown_label] = '-'  # normalize unknown
        # Assume every mat file in the relevant directory is a segmentation.
        try:
            self.segs = sorted([n for n in os.listdir(
                os.path.join(directory, self.partdir, 'Annotations_Part'))
                                if n.endswith('.mat')])
        except OSError:
            message = 'Error when searching for pascal part annotations, please check your dataset.' \
                        + ' With this error you may only use testing scripts. Training will fail unless you resolve this warning.'
            warnings.warn(message)

    def all_names(self, category, j):
        if category == 'object':
            return [self.object_names[j]]
        elif category == 'part':
            # TODO: include left/right synonyms that have been collapsed
            return [self.part_names[j]]
        else:
            print('Could not find', category, j)
            raise ValueError

    def size(self):
        return len(self.segs)

    def filename(self, i):
        # Translate the segmentation mat file name to the .jpg filename
        return os.path.join(self.directory, self.version, 'JPEGImages',
                            self.segs[i].replace('.mat', '.jpg'))

    def metadata(self, i):
        return (self.directory, self.partdir, self.contextdir, \
                self.part_key, self.segs[i])

    @classmethod
    def resolve_segmentation(cls, m, categories=None):
        directory, partdir, contextdir, part_key, seg_fn = m
        result = {}
        if wants('part', categories):
            objs, parts = load_parts_segmentation(
                os.path.join(directory, partdir, 'Annotations_Part', seg_fn),
                part_key)  # (ignore object layer the from parts segmentation)
            result['part'] = parts
        if wants('object', categories):
            result['object'] = loadmat(os.path.join(directory, contextdir, 'trainval', seg_fn))['LabelMap']
        arrs = [a for a in list(result.values()) if len(numpy.shape(a)) >= 2]
        shape = arrs[0].shape[-2:] if arrs else (1, 1)
        return result, shape


def load_parts_segmentation(filename, part_key):
    """
    Processes a single Annotations_Part annotation into two arrays:
    an object class segmentation, and a part segmentation.  We discard
    instance information for now.  If no objects are present, returns (0, 0).
    """
    d = loadmat(filename)
    instance_count = d['anno'][0, 0]['objects'].shape[1]
    # We need at least one instance annotated
    if not instance_count:
        return (0, 0)
    mask_shape = d['anno'][0, 0]['objects'][0, 0]['mask'].shape
    # We will merge all objects and parts into these two layers
    object_seg = numpy.zeros(mask_shape, dtype=numpy.int16)
    part_seg = numpy.zeros(mask_shape, dtype=numpy.int16)
    for i in range(instance_count):
        obj = d['anno'][0, 0]['objects'][0, i]
        object_ind = obj['class_ind'][0, 0]
        object_seg[obj['mask'].astype(numpy.bool)] = object_ind
        part_count = obj['parts'].shape[1]
        for i in range(part_count):
            part = obj['parts'][0, i]
            part_name = part['part_name'][0]
            part_code = part_key[(object_ind, part_name)]
            part_seg[part['mask'].astype(numpy.bool)] = part_code
    return object_seg, part_seg


def normalize_part_key(raw_names):
    """
    Enforces a coding policy: all the multiple part names such as 'engine 3'
    are just called 'engine', and they are aliased down to the same part code.
    Returns names of object classes, parts, and a part_key mapping raw
    (object, part) number pair tuples to the canonical integer part code.
    """
    # Identify the maximum object and part indexes used.
    object_class_count = max(c for c, p in raw_names) + 1
    # Create a list of object names, a list of part names, and a part key
    object_class_names = ['-'] * object_class_count
    part_class_names = ['-']
    # alias_key map (collapsed obj name, collapsed part name) -> assigned part index. 
    alias_key = {}
    part_key = {}

    for (c, p) in sorted(list(raw_names.keys())):
        n = raw_names[(c, p)]
        object_class_names[c] = n[0]
        # Alias codes that have the same name such as 'airplane engine' (1,2,3)
        if n in alias_key:
            code = alias_key[n]
        else:
            if n[1] in part_class_names:
                code = part_class_names.index(n[1])
            else:
                code = len(part_class_names)
                part_class_names.append(n[1])
            alias_key[n] = code
        part_key[(c, p)] = code
    return object_class_names, part_class_names, part_key


def normalize_all_readable(raw_keys, collapse_adjectives):
    return dict((k, (normalize_readable(c, collapse_adjectives),
                     normalize_readable(p, collapse_adjectives)))
                for k, (c, p) in list(raw_keys.items()))


def normalize_readable(name, collapse_adjectives):
    # Long names for short part names that are unexplained in the file.
    decoded_names = dict(
        lfho='left front hoof',
        rfho='right front hoof',
        lbho='left back hoof',
        rbho='right back hoof',
        fwheel='front wheel',
        bwheel='back wheel',
        frontside='front side',
        leftside='left side',
        rightside='right side',
        backside='back side',
        roofside='roof side',
        leftmirror='left mirror',
        rightmirror='right mirror'
    )
    if name in decoded_names:
        name = decoded_names[name]
    # Other datasets use 'airplane'.
    name = name.replace('aeroplane', 'airplane')
    # If we need to remove adjectives like 'left' and 'right', do so now.h
    if collapse_adjectives is not None:
        name = ' '.join(n for n in name.split() if n not in collapse_adjectives)
    return name


def load_context_labels(labels_filename):
    """
    Parses labels.txt from the PASCAL Context distribution;
    this contains an "index: name" mapping.
    """
    with open(labels_filename, 'r') as labels_file:
        lines = [s.strip().split(': ') for s in labels_file.readlines()]
    pairs = [(int(i), n) for i, n in lines]
    object_names = ['-'] * (max(i for i, n in pairs) + 1)
    for i, n in pairs:
        object_names[i] = n
    return object_names


def load_part2ind(part2ind_filename):
    """
    Parses the part2ind.m file from the PASCAL Parts distribution
    just as a series of line patterns, returning a map from (object, part)
    number paris to (objectname, partname) names.
    """
    import re
    from collections import OrderedDict
    with open(part2ind_filename, 'r') as part2ind_file:
        lines = part2ind_file.readlines()
    result = OrderedDict()
    for line in lines:
        # % [aeroplane]
        m = re.match('^% \[([^\]]*)\]', line)
        if m:
            object_name = m.group(1)
            continue
        # pimap{1}('lwing')       = 3;                % left wing
        m = re.match(r'''(?x)^
            pimap\{(\d+)\}            # group 1: the object index
            \('([^']*)'\)             # group 2: the part short name
            \s*=\s*                   # equals
            (\d+);                    # group 3: the part number
            (?:\s*%\s*(\w[\w ]*\w))?  # group 4: the part long name
            ''', line)
        if m:
            part_name = m.group(2)
            readable_name = m.group(4) or part_name
            object_index = int(m.group(1))
            part_index = int(m.group(3))
            # ('aeroplane', 'left wing')
            result[(object_index, part_name)] = (
                object_name, readable_name)
            continue
        # for ii = 1:10
        m = re.match(r'for ii = 1:(\d+)*', line)
        if m:
            iteration_count = int(m.group(1))
            continue

        # pimap{1}(sprintf('engine_%d', ii)) = 10+ii; % multiple engines
        m = re.match(r'''(?x)^\s*
            pimap\{ (\d+) \}          # group 1: the object index
            \(sprintf\('
                ([^']*)_%d            # group 2: the short part name
            ',\s*ii\)\)
            \s*=\s*                   # equals
            (\d+)                     # group 3: the part number
            \s*\+\s*ii\s*;            # plus indexing
            (?:\s*%\s*(\w[\w ]*\w))?  # group 4: the multiple-part name
            ''', line)
        if m:
            # Deal with ranges
            part_name = m.group(2)
            # Take a multiple name if it does not say 'multiple' in it.
            readable_name = part_name
            if m.group(4) and 'multiple' not in m.group(4):
                readable_name = m.group(4)
            object_index = int(m.group(1))
            first = int(m.group(3)) + 1  # one-based indexing
            for part_index in range(1, 1 + iteration_count):
                result[(object_index, part_name + '_%d' % part_index)] = (
                    object_name, readable_name)
            continue

        # % only has sihouette mask
        m = re.match(r'% only has si', line)  # (misspelled in file)
        if m:
            object_index += 1
            result[(object_index, 'silhouette')] = (
                object_name, 'silhouette')

        # keySet = keys(pimap{8});
        # valueSet = values(pimap{8});
        m = re.match(r'''(?x)^
            (key|value)Set\s*=\s*\1s\(pimap{ # group 1: key or value
            (\d+)                            # group 2: old object number
            }\);
            ''', line)
        if m:
            object_to_copy = int(m.group(2))
            continue

        # pimap{12} = containers.Map(keySet, valueSet);
        m = re.match(r'''(?x)^
            pimap{
            (\d+)                           # group 1: new object number
            }\s*=\s*containers.Map\(keySet,\s*valueSet\);
            ''', line)
        if m:
            object_index = int(m.group(1))
            for (other_obj, part_index), val in list(result.items()):
                if other_obj == object_to_copy:
                    result[(object_index, part_index)] = (object_name, val[1])
            continue
        # recognize other lines that can be ignored
        m = re.match(r'''(?x)^(?:
                function|
                pimap\ =|
                \s*pimap\{ii\}|
                end|
                \s*%|
                remove|
                $)''', line)
        if m:
            continue
        print('unrecognized line', line)
        import sys
        sys.exit(1)
    return result


def wants(what, option):
    if option is None:
        return True
    return what in option

