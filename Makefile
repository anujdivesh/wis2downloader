docs:
	bash docs/build.sh site

docs-dev:
	docker build --target docs-builder -t wis2-docs-builder -f containers/ui/Dockerfile .
	mkdir -p modules/ui/site
	docker run --rm -v "$(CURDIR)/modules/ui/site:/out" wis2-docs-builder cp -a /repo/site/. /out/
