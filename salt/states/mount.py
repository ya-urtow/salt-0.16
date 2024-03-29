'''
Mounting of filesystems.
========================

Mount any type of mountable filesystem with the mounted function:

.. code-block:: yaml

    /mnt/sdb:
      mount.mounted:
        - device: /dev/sdb1
        - fstype: ext4
        - mkmnt: True
        - opts:
          - defaults
'''

# Import salt libs
from salt._compat import string_types


def mounted(name,
            device,
            fstype,
            mkmnt=False,
            opts=None,
            dump=0,
            pass_num=0,
            config='/etc/fstab',
            persist=True):
    '''
    Verify that a device is mounted

    name
        The path to the location where the device is to be mounted

    device
        The device name, typically the device node, such as /dev/sdb1

    fstype
        The filesystem type, this will be xfs, ext2/3/4 in the case of classic
        filesystems, and fuse in the case of fuse mounts

    mkmnt
        If the mount point is not present then the state will fail, set mkmnt
        to True to create the mount point if it is otherwise not present

    opts
        A list object of options or a comma delimited list

    dump
        The dump value to be passed into the fstab, default to 0

    pass_num
        The pass value to be passed into the fstab, default to 0

    config
        Set an alternative location for the fstab, default to /etc/fstab

    remount
        Set if the file system can be remounted with the remount option,
        default to True

    persist
        Set if the mount should be saved in the fstab, default to True
    '''
    ret = {'name': name,
           'changes': {},
           'result': True,
           'comment': ''}

    # Make sure that opts is correct, it can be a list or a comma delimited
    # string
    if isinstance(opts, string_types):
        opts = opts.split(',')
    elif opts is None:
        opts = ['defaults']

    # Get the active data
    active = __salt__['mount.active']()
    if name not in active:
        # The mount is not present! Mount it
        if __opts__['test']:
            ret['result'] = None
            ret['comment'] = '{0} would be mounted'.format(name)
            return ret

        out = __salt__['mount.mount'](name, device, mkmnt, fstype, opts)
        active = __salt__['mount.active']()
        if isinstance(out, string_types):
            # Failed to (re)mount, the state has failed!
            ret['comment'] = out
            ret['result'] = False
            return ret
        elif name in active:
            # (Re)mount worked!
            ret['comment'] = 'Target was successfully mounted'
            ret['changes']['mount'] = True
    else:
        ret['comment'] = 'Target was already mounted'

    if persist and name in active:
        if __opts__['test']:
            out = __salt__['mount.set_fstab'](name,
                                              device,
                                              fstype,
                                              opts,
                                              dump,
                                              pass_num,
                                              config,
                                              test=True)
            if out != 'present':
                ret['result'] = None
                if out == 'new':
                    ret['comment'] = ('{0} is mounted, but needs to be '
                                      'written to the fstab in order to be '
                                      'made persistent').format(name)
                elif out == 'change':
                    ret['comment'] = ('{0} is mounted, but its fstab entry '
                                      'must be updated').format(name)
                else:
                    ret['result'] = False
                    ret['comment'] = ('Unable to detect fstab status for '
                                      'mount point {0} due to unexpected '
                                      'output \'{1}\' from call to '
                                      'mount.set_fstab. This is most likely '
                                      'a bug.').format(name, out)
                return ret

        else:
            out = __salt__['mount.set_fstab'](name,
                                              device,
                                              fstype,
                                              opts,
                                              dump,
                                              pass_num,
                                              config)

        if out == 'present':
            return ret
        if out == 'new':
            ret['changes']['persist'] = 'new'
            ret['comment'] += '. Added new entry to the fstab.'
            return ret
        if out == 'change':
            ret['changes']['persist'] = 'update'
            ret['comment'] += '. Updated the entry in the fstab.'
            return ret
        if out == 'bad config':
            ret['result'] = False
            ret['comment'] += '. However, the fstab was not found.'
            return ret

    return ret


def swap(name, persist=True, config='/etc/fstab'):
    '''
    Activates a swap device

    .. code-block:: yaml

        /root/swapfile:
          mount.swap
    '''
    ret = {'name': name,
           'changes': {},
           'result': True,
           'comment': ''}
    on_ = __salt__['mount.swaps']()

    if name in on_:
        ret['comment'] = 'Swap {0} already active'.format(name)
    elif __opts__['test']:
        ret['result'] = None
        ret['comment'] = 'Swap {0} is set to be activated'.format(name)
    else:
        __salt__['mount.swapon'](name)

        on_ = __salt__['mount.swaps']()

        if name in on_:
            ret['comment'] = 'Swap {0} activated'.format(name)
            ret['changes'] = on_[name]
        else:
            ret['comment'] = 'Swap {0} failed to activate'.format(name)
            ret['result'] = False

    if persist:
        fstab_data = __salt__['mount.fstab'](config)
        if __opts__['test']:
            if name not in fstab_data:
                ret['result'] = None
                if name in on_:
                    ret['comment'] = ('Swap {0} is set to be added to the '
                                      'fstab and to be activated').format(name)
            return ret

        if 'none' in fstab_data:
            if fstab_data['none']['device'] == name:
                return ret

        # present, new, change, bad config
        # Make sure the entry is in the fstab
        out = __salt__['mount.set_fstab']('none',
                                          name,
                                          'swap',
                                          ['defaults'],
                                          0,
                                          0,
                                          config)
        if out == 'present':
            return ret
        if out == 'new':
            ret['changes']['persist'] = 'new'
            ret['comment'] += '. Added new entry to the fstab.'
            return ret
        if out == 'change':
            ret['changes']['persist'] = 'update'
            ret['comment'] += '. Updated the entry in the fstab.'
            return ret
        if out == 'bad config':
            ret['result'] = False
            ret['comment'] += '. However, the fstab was not found.'
            return ret
    return ret
