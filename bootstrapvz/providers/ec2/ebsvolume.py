from bootstrapvz.base.fs.volume import Volume
from bootstrapvz.base.fs.exceptions import VolumeError
from bootstrapvz.common.tools import log_check_call


class EBSVolume(Volume):

    def create(self, conn, zone, tags=[], encrypted=False, kms_key_id=None):
        self.fsm.create(connection=conn, zone=zone, tags=tags, encrypted=encrypted, kms_key_id=kms_key_id)

    def _before_create(self, e):
        self.conn = e.connection
        zone = e.zone
        tags = e.tags
        size = self.size.bytes.get_qty_in('GiB')

        params = {
            'Size': size,
            'AvailabilityZone': zone,
            'VolumeType': 'gp2',
        }
        if tags:
            params['TagSpecifications'] = [{'ResourceType': 'volume', 'Tags': tags}]

        if e.encrypted:
            params['Encrypted'] = e.encrypted
            if e.kms_key_id:
                params['KmsKeyId'] = e.kms_key_id

        self.volume = self.conn.create_volume(**params)

        self.vol_id = self.volume['VolumeId']
        waiter = self.conn.get_waiter('volume_available')
        waiter.wait(VolumeIds=[self.vol_id],
                    Filters=[{'Name': 'status', 'Values': ['available']}])

    def attach(self, instance_id):
        self.fsm.attach(instance_id=instance_id)

    def _before_attach(self, e):
        import os.path
        import string
        import urllib2

        def name_mapped(path):
            return path.split('/')[-1].replace('xvd', 'sd')[:3]

        self.instance_id = e.instance_id

        dev_map_names = set()
        launch_map_url = 'http://169.254.169.254/latest/meta-data/block-device-mapping/'
        launch_map_response = urllib2.urlopen(url=launch_map_url, timeout=5)
        for map_name in [d.strip() for d in launch_map_response.readlines()]:
            dev_url = launch_map_url + map_name
            dev_response = urllib2.urlopen(url=dev_url, timeout=5)
            dev_map_names.add(name_mapped(dev_response.read().strip()))

        try:
            instance = self.conn.describe_instances(
                Filters=[
                    {'Name': 'instance-id', 'Values': [self.instance_id]}
                ]
            )['Reservations'][0]['Instances'][0]
        except (IndexError, KeyError):
            raise VolumeError('Unable to fetch EC2 instance volume data')

        for mapped_dev in instance.get('BlockDeviceMappings', list()):
            dev_map_names.add(name_mapped(mapped_dev['DeviceName']))

        for letter in reversed(string.ascii_lowercase[1:]):
            if 'sd' + letter not in dev_map_names:
                self.ec2_device_path = '/dev/sd' + letter
                break

        if self.ec2_device_path is None:
            raise VolumeError('Unable to find a free block device mapping for bootstrap volume')

        self.device_path = None

        lsblk_command = ['lsblk', '--noheadings', '--list', '--nodeps', '--output', 'NAME']

        lsblk_start = log_check_call(lsblk_command)
        start_dev_names = set(lsblk_start)

        self.conn.attach_volume(VolumeId=self.vol_id,
                                InstanceId=self.instance_id,
                                Device=self.ec2_device_path)
        waiter = self.conn.get_waiter('volume_in_use')
        waiter.wait(VolumeIds=[self.vol_id],
                    Filters=[{'Name': 'attachment.status', 'Values': ['attached']}])

        log_check_call(['udevadm', 'settle'])

        lsblk_end = log_check_call(lsblk_command)
        end_dev_names = set(lsblk_end)

        if len(start_dev_names ^ end_dev_names) != 1:
            raise VolumeError('Could not determine the device name for bootstrap volume')

        udev_name = (start_dev_names ^ end_dev_names).pop()
        udev_path = log_check_call(['udevadm', 'info',
                                    '--root', '--query=name', '--name',
                                    udev_name])
        if len(udev_path) != 1 or not os.path.exists(udev_path[0]):
            raise VolumeError('Could not find device path for bootstrap volume')

        self.device_path = udev_path[0]

    def _before_detach(self, e):
        self.conn.detach_volume(VolumeId=self.vol_id,
                                InstanceId=self.instance_id,
                                Device=self.ec2_device_path)
        waiter = self.conn.get_waiter('volume_available')
        waiter.wait(VolumeIds=[self.vol_id],
                    Filters=[{'Name': 'status', 'Values': ['available']}])
        del self.ec2_device_path
        self.device_path = None

    def _before_delete(self, e):
        self.conn.delete_volume(VolumeId=self.vol_id)

    def snapshot(self):
        snapshot = self.conn.create_snapshot(VolumeId=self.vol_id)
        self.snap_id = snapshot['SnapshotId']
        waiter = self.conn.get_waiter('snapshot_completed')
        waiter.wait(SnapshotIds=[self.snap_id],
                    Filters=[{'Name': 'status', 'Values': ['completed']}],
                    WaiterConfig={'Delay': 15, 'MaxAttempts': 120})
        return self.snap_id
