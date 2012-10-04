# The hash DAG
#
# Copyright (C) 2012 Peter Todd <pete@petertodd.org>
#
# This file is part of OpenTimestamps.
#
# OpenTimestamps is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by the
# Free Software Foundation; either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import binascii
import hashlib
import time
import re

from . import serialization

# Operations - edges in DAG
# Digests - vertexes in DAG

class __MasterOpSerializer(serialization.Serializer):
    instantiator = None

    @classmethod
    def __create_arguments_dict(cls,obj):
        r = {}
        for arg_name in obj.__class__.op_arguments:
            try:
                r[arg_name] = getattr(obj,arg_name)
            except AttributeError:
                raise AttributeError("Missing attribute '%s' from %r instance" %\
                        (arg_name,obj.__class__))

        # inputs are handled specially. Rather than serializing the actual Op
        # objects themselves, serialize the object's digests instead.
        r['inputs'] = tuple(i.digest for i in obj.inputs)

        return r

    @classmethod
    def json_serialize(cls,obj):
        arg_dict = cls.__create_arguments_dict(obj)
        return {obj.__class__.op_name:
                    serialization.DictSerializer.json_serialize(arg_dict)}

    @classmethod
    def json_deserialize(cls,json_obj):
        args_dict = serialization.DictSerializer.json_deserialize(json_obj,do_typed_object_hack=False) 
        return cls.instantiator(**args_dict)

    @classmethod
    def _binary_serialize(cls,obj,fd):
        args_dict = cls.__create_arguments_dict(obj)
        serialization.DictSerializer._binary_serialize(args_dict,fd)

    @classmethod
    def _binary_deserialize(cls,fd):
        args_dict = serialization.DictSerializer._binary_deserialize(fd)
        return cls.instantiator(**args_dict)

def register_Op(cls):
    # We don't support multiple inheritence for ops. If we did the following
    # code would need to be changed. Probably better to avoid having to use
    # multiple inheritence, as if we switch to Cython later the resulting
    # compiled code is a lot faster.
    assert len(cls.__bases__) == 1

    # Process the op arguments, adding arguments from base classes and ensuring
    # the arguments make sense. 

    all_args = {}

    # Inherit arguments from the base class
    if issubclass(cls.__base__,Op):
        all_args.update(cls.__base__.op_arguments)

    for arg_name in cls.op_arguments:
        if arg_name in all_args: 
            raise ValueError(\
"Argument name '%s' defined in %r has the same name as an argument in base class %r" %\
                    (arg_name,cls,all_args[arg_name]))
        else:
            all_args[arg_name] = cls

    cls.op_arguments = all_args

    # Create a serialization class for the Op class to allow it to be
    # serialized.
    class new_op_serializer(__MasterOpSerializer):
        type_name = cls.op_name
        typecode_byte = serialization.typecodes_by_name[type_name]
        auto_serialized_classes = (cls,)
        instantiator = cls

    # Change the name to something meaningful. Otherwise they'll all have the
    # name 'new_op_serializer'; not very useful for debugging.
    new_op_serializer.__name__ = '%sType' % cls.op_name

    serialization.register_serializer(new_op_serializer)

    return cls


class Op(object):
    op_name = 'Op'
    op_arguments = ('digest','inputs',)

    def _preinit(self,inputs=(),dag=None,**kwargs):
        if dag is None:
            dag = null_dag
        self.dag = dag
        self.inputs = self.dag.link_inputs(inputs,self) 

    def __init__(self,inputs=(),digest=None,dag=None,**kwargs):
        # FIXME: this removing this through
        #
        #self._preinit(inputs=inputs,**kwargs)

        self.digest = digest

        # Note that if dag isn't a keyword argument above, it'll be in kwargs,
        # which means that the usual dag=None stuff will update the correctly
        # set dag...
        self.__dict__.update(kwargs)

        if self.digest is None:
            self.digest = self._calc_digest()

        self.dependent_ops = set()
        self.dag.link_output(self)

        # You might think we should check that the argument values match their
        # types now. However we leave that until the object is serialized as
        # right now self.inputs is a list of Digest's, not bytes. 

    def __eq__(self,other):
        """Equality comparison.

        Equality is defined by what would cause the digest to be different, so
        just compare the digests directly.
        """
        try:
            return self.digest == other.digest
        except AttributeError:
            return False

# Done here to avoid needing a forward declaration
Op = register_Op(Op)

@register_Op
class Digest(Op):
    op_name = 'Digest'
    op_arguments = ()

    def __init__(self,digest=None,inputs=(),dag=None):
        self._preinit(digest=digest,inputs=inputs,dag=dag)

        if digest is None:
            raise ValueError('Must specify digest value')
        elif not isinstance(digest,bytes):
            raise TypeError('digest must be of type bytes')

        if len(inputs) > 0:
            raise ValueError("Digest Op's can not have inputs")

        super(Digest,self).__init__(digest=digest,dag=dag)

@register_Op
class Hash(Op):
    op_name = 'Hash'
    op_arguments = ('algorithm',)

    def __init__(self,algorithm=u'sha256d',**kwargs):
        self._preinit(**kwargs)

        if algorithm not in (u'sha256d',u'sha1d',u'sha512d',u'crc32'):
            raise ValueError('Unsupported hash algorithm %s' % algorithm)
        self.algorithm = algorithm

        super(Hash,self).__init__(algorithm=algorithm,**kwargs)

    def _calc_digest(self):
        hash_fn = None
        if self.algorithm == 'crc32':
            # Quick-n-dirty crc32 implementation
            class hash_crc32(object):
                def update(self,newdata):
                    self.crc = binascii.crc32(newdata,self.crc)
                def __init__(self,data=''):
                    self.crc = 0
                    self.update(data)
                def digest(self):
                    import struct
                    return struct.pack('>L',self.crc)
            hash_fn = hash_crc32
        else:
            hash_fn = getattr(hashlib,self.algorithm[0:-1])

        h = hash_fn() 
        for i in self.inputs:
            h.update(i.digest)

        # Ugly way of determining if we need to hash things twice.
        if self.algorithm[-1] == 'd':
            return hash_fn(h.digest()).digest()
        else:
            return h.digest()

# Timestamps are interpreted as microseconds since the epoch, mainly so
# javascript can represent timestamps exactly with it's 2^53 bits available for
# ints.
def time_to_timestamp(t):
    return int(t * 1000000)

def time_from_timestamp(t):
    return t / 1000000.0

# The regex that valid notary method names must match.
#
# Basically, first character must be alphabetical. Second character must exist
# and may also have numbers or the characters _ - or .
#
# Unicode characters are not allowed.
valid_notary_method_name_regex = '^[A-Za-z][A-Za-z0-9_\-\.]+$'
valid_notary_method_name_re = re.compile(valid_notary_method_name_regex)

@register_Op
class Verify(Op):
    op_name = 'Verify'
    op_arguments =\
            ('timestamp',
             'notary_method',
             'notary_method_version',
             'notary_identity',
             'notary_args')

    # These arguments are used to computer the digest
    hashed_arguments = ('inputs','timestamp',
                        'notary_method','notary_method_version',
                        'notary_identity','notary_args')

    def __init__(self,inputs=(),
            timestamp=None,
            notary_method=None,
            notary_method_version=0,
            notary_identity=u'',
            notary_args={},
            **kwargs):
        self._preinit(inputs=inputs,**kwargs)

        if len(inputs) != 1:
            raise ValueError('Verify operations must have exactly one input, got %d' % len(inputs))

        if timestamp is None:
            timestamp = time_to_timestamp(time.time())

        if not (isinstance(timestamp,int) or isinstance(timestamp,long)):
            raise TypeError("Timestamp must be an integer")
        elif timestamp < 0:
            raise ValueError("Timestamp must be a positive integer")

        # Note that creating a timestamp in the past is not an error to allow
        # the import of timestamps from other timestamping systems.

        if notary_method is None:
            raise ValueError("notary_method not specified")
        elif re.match(valid_notary_method_name_re,notary_method) is None:
            raise ValueError("notary_method must match the regex '%s', got %r" %
                    (valid_notary_method_name_regex,notary_method))

        if not isinstance(notary_method_version,int):
            raise TypeError("notary_method_version must be an integer")
        elif notary_method_version < 0:
            raise ValueError("notary_method_version must be >= 0")

        self.timestamp = int(timestamp)
        self.notary_method = unicode(notary_method)
        self.notary_method_version = int(notary_method_version)
        self.notary_identity = unicode(notary_identity)
        self.notary_args = notary_args

        super(Verify,self).__init__(inputs,**kwargs)

    def _calc_digest(self):
        digest_dict = {}
        for hashed_key in self.hashed_arguments: 
            digest_dict[hashed_key] = getattr(self,hashed_key)
        return serialization.DictSerializer.binary_serialize(digest_dict)

    def verify(self):
        raise TypeError("Can't verify; unknown notary method %s" % self.notary_method)


class Dag(object):
    """Keep track of what inputs and outputs are connected"""

    def link_inputs(self,input_digests,op):
        r = []
        for i in input_digests:
            if isinstance(i,bytes):
                r.append(Digest(digest=i,dag=self))
            elif isinstance(i,Op):
                r.append(i) 
            else:
                raise TypeError(\
                    "Invalid input digest, expected bytes or Op subclass, got %r" % i.__class__)
        return tuple(r) 

    def link_output(self,op):
        if not isinstance(op.digest,bytes):
            raise TypeError(\
                "Invalid output digest, expected bytes, got %r" % output_digest.__class__)

null_dag = Dag()

class MemoryDag(Dag):
    digests = None

    def __init__(self):
        self.digests = {}

    def __link_digest(self,new_digest_obj,op):
        if isinstance(new_digest_obj,bytes):
            if new_digest_obj in self.digests:
                # digest already known
                return self.digests[new_digest_obj]
            else:
                # Not known, create new Digest obj to hold it. Note how we set
                # the dag to null_dag and change it later. This triggers a
                # special case in the null_dag's link_output function, to avoid
                # recursively calling Digest again.
                new_digest_obj = Digest(digest=new_digest_obj,dag=null_dag)
                new_digest_obj.dag = self
                self.digests[new_digest_obj.digest] = new_digest_obj
                return new_digest_obj

        elif isinstance(new_digest_obj,Op):
            # We can take ownership if the new digest object is part of the null_dag
            if new_digest_obj.dag is null_dag:
                new_digest_obj.dag = self
            elif new_digest_obj.dag is not self:
                # Object is already part of another dag. Fail for now, we can
                # decide later if allowing this usage is a good thing.
                assert False

            old_digest_obj = self.digests.get(new_digest_obj.digest)
            if old_digest_obj is None:
                # Unknown digest, so just add it directly.
                self.digests[new_digest_obj.digest] = new_digest_obj
                return new_digest_obj
            else:
                # We already know about this digest. Why?
                if old_digest_obj is new_digest_obj:
                    return new_digest_obj

                elif isinstance(new_digest_obj,Digest):
                    # All we have is a Digest, so whatever is already in the
                    # Dag is better.
                    return old_digest_obj

                elif isinstance(old_digest_obj,Digest):
                    # The Dag has a straight up Digest object, while we have
                    # something better. Add ours instead, and change the inputs
                    # of every object that referenced the old digest to the new
                    # digest.
                    for op in old_digest_obj.dependent_ops:
                        new_digest_obj.dependent_ops.add(op)
                        for (op_digest,op_digest_idx) in enumerate(op.inputs):
                            if op_digest is old_digest_obj:
                                op.inputs[op_digest_idx] = new_digest_obj
                    old_digest_obj.dependent_ops = set()
                    self.digests[new_digest_obj.digest] = new_digest_obj
                    return new_digest_obj
                
                else:
                    # Both the old and new digests we know about are not simple
                    # Digest objects.
                    if old_digest_obj == new_digest_obj:
                        # The two are equivalent in value, but aren't the same
                        # object. Merge both objects into one.
                        assert False # FIXME: implement this
                    else:
                        raise AssertionError(\
                                "Found two different Op objects with the same digest. "
                                "This should never happen.")

        else:
            raise TypeError(\
                "Invalid digest, expected bytes or Op subclass, got %r" % new_digest_obj.__class__)


    def link_inputs(self,input_digests,op):
        r = []
        for i in input_digests:
            r.append(self.__link_digest(i,op))
            r[-1].dependent_ops.add(op)
        return r

    def link_output(self,op):
        return self.__link_digest(op,op)
