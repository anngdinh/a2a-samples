# sudo apt-get install docker-buildx-plugin

.PHONY: build-docker-app build-docker-ui

# Docker image names
APP_IMAGE_NAME = vcr.vngcloud.vn/60108-annd2-ingress/langgraph-agent-app
UI_IMAGE_NAME = vcr.vngcloud.vn/60108-annd2-ingress/langgraph-agent-ui
TAG ?= v0.0.0

# Build the main application Docker image
build-docker-app:
	@echo "Building application Docker image..."
	DOCKER_BUILDKIT=1 docker build -f Containerfile -t $(APP_IMAGE_NAME):$(TAG) .
# 	docker push $(APP_IMAGE_NAME):$(TAG)
	@echo "Successfully built $(APP_IMAGE_NAME):$(TAG)"

# Build the main application Docker image for multiple platforms
build-docker-app-multiplatform:
	@echo "Building application Docker image for multiple platforms..."
	docker buildx build --platform linux/amd64,linux/arm64 -f Containerfile -t $(APP_IMAGE_NAME):$(TAG) --push .
	@echo "Successfully built and pushed $(APP_IMAGE_NAME):$(TAG) for multiple platforms"

# Build the Streamlit UI Docker image
build-docker-ui:
	@echo "Building Streamlit UI Docker image..."
	DOCKER_BUILDKIT=1 docker build -f Containerfile.streamlit -t $(UI_IMAGE_NAME):$(TAG) .
# 	docker push $(UI_IMAGE_NAME):$(TAG)
	@echo "Successfully built $(UI_IMAGE_NAME):$(TAG)"

# Build the Streamlit UI Docker image for multiple platforms
build-docker-ui-multiplatform:
	@echo "Building Streamlit UI Docker image for multiple platforms..."
	docker buildx build --platform linux/amd64,linux/arm64 -f Containerfile.streamlit -t $(UI_IMAGE_NAME):$(TAG) --push .
	@echo "Successfully built and pushed $(UI_IMAGE_NAME):$(TAG) for multiple platforms"
