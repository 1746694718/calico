.PHONY: all binary test plugin ipam ut clean update-version

# The current CNI repo version.
CALICO_CNI_VERSION=v1.0.2-dev

SRCFILES=$(shell find calico_cni) calico.py ipam.py
LOCAL_IP_ENV?=$(shell ip route get 8.8.8.8 | head -1 | cut -d' ' -f8)

default: all
all: binary test
binary: dist/calico dist/calico-ipam
test: ut fv
plugin: dist/calico
ipam: dist/calico-ipam


# Builds the Calico CNI plugin binary.
dist/calico: $(SRCFILES) update-version 
	docker run  --rm \
	-v `pwd`:/code \
	calico/build:v0.11.0 \
	pyinstaller calico.py -ayF

# Makes the IPAM plugin.
dist/calico-ipam: $(SRCFILES) update-version
	docker run --rm \
	-v `pwd`:/code \
	calico/build:v0.11.0 \
	pyinstaller ipam.py -ayF -n calico-ipam

# Updates the version information in version.py
update-version:
	echo "# Auto-generated contents.  Do not manually edit" > calico_cni/version.py
	echo "# or check in this file." >> calico_cni/version.py
	echo "__version__ = '$(shell git describe --tags)'" >> calico_cni/version.py
	echo "__commit__ = '$(shell git rev-parse HEAD)'" >> calico_cni/version.py 
	echo "__branch__ = '$(shell git rev-parse --abbrev-ref HEAD)'" >> calico_cni/version.py

# Copy the plugin into place
deploy-rkt: dist/calicoctl
	cp dist/calico /etc/rkt/net.d

# Run the unit tests.
ut: update-version
	docker run --rm -v `pwd`:/code \
	calico/test \
	nosetests tests/unit -c nose.cfg

# Run the fv tests.
fv: update-version
	docker run --rm -v `pwd`:/code \
	calico/test \
	nosetests tests/fv -c nose.cfg

# Makes tests on Circle CI.
test-circle: update-version 
	# Can't use --rm on circle
	# Circle also requires extra options for reporting.
	docker run \
	-v `pwd`:/code \
	-v $(CIRCLE_TEST_REPORTS):/circle_output \
	-e COVERALLS_REPO_TOKEN=$(COVERALLS_REPO_TOKEN) \
	calico/test sh -c \
	'nosetests tests -c nose.cfg \
	--with-xunit --xunit-file=/circle_output/output.xml; RC=$$?;\
	[[ ! -z "$$COVERALLS_REPO_TOKEN" ]] && coveralls || true; exit $$RC'

clean:
	-rm -f *.created
	find . -name '*.pyc' -exec rm -f {} +
	-sudo rm -rf dist
	-docker run -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker:/var/lib/docker --rm martin/docker-cleanup-volumes
	rm -f calico_cni/version.py

## Run etcd in a container. Generally useful.
run-etcd:
	@-docker rm -f calico-etcd
	docker run --detach \
	--net=host \
	--name calico-etcd quay.io/coreos/etcd:v2.2.2 \
	--advertise-client-urls "http://$(LOCAL_IP_ENV):2379,http://127.0.0.1:2379,http://$(LOCAL_IP_ENV):4001,http://127.0.0.1:4001" \
	--listen-client-urls "http://0.0.0.0:2379,http://0.0.0.0:4001"

