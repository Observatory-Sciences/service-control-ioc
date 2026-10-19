"""Static introspection XML for the systemd D-Bus members the observer uses.

Only the members we call or listen to are declared. Signatures must match
systemd exactly: dbus-fast drops a signal whose signature differs.
"""

from typing import Final

MANAGER_XML: Final = """\
<node>
 <interface name="org.freedesktop.systemd1.Manager">
  <method name="Subscribe"/>
  <method name="ListUnitsByNames">
   <arg type="as" name="names" direction="in"/>
   <arg type="a(ssssssouso)" name="units" direction="out"/>
  </method>
  <signal name="JobNew">
   <arg type="u" name="id"/>
   <arg type="o" name="job"/>
   <arg type="s" name="unit"/>
  </signal>
  <signal name="JobRemoved">
   <arg type="u" name="id"/>
   <arg type="o" name="job"/>
   <arg type="s" name="unit"/>
   <arg type="s" name="result"/>
  </signal>
  <signal name="Reloading">
   <arg type="b" name="active"/>
  </signal>
 </interface>
</node>
"""

PROPERTIES_XML: Final = """\
<node>
 <interface name="org.freedesktop.DBus.Properties">
  <method name="GetAll">
   <arg type="s" name="interface_name" direction="in"/>
   <arg type="a{sv}" name="props" direction="out"/>
  </method>
  <signal name="PropertiesChanged">
   <arg type="s" name="interface_name"/>
   <arg type="a{sv}" name="changed_properties"/>
   <arg type="as" name="invalidated_properties"/>
  </signal>
 </interface>
</node>
"""
