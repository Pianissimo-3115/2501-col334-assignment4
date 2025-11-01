sudo mn -c
sudo pkill -9 -f "ryu|iperf|mnexec|controller"
for b in $(sudo ovs-vsctl list-br); do sudo ovs-vsctl del-br "$b"; done
sudo systemctl restart openvswitch-switch || sudo service openvswitch-switch restart
clear
