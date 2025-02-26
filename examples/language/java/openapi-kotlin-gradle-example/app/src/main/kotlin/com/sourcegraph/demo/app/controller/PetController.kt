package com.sourcegraph.demo.app.controller

import com.sourcegraph.demo.app.service.PetService
import com.sourcegraph.demo.openapi.generated.model.Pet
import io.ktor.application.*
import io.ktor.http.*
import io.ktor.response.*
import io.ktor.routing.*

class PetController(private val petService: PetService) {

    fun setupRoutes(routing: Routing) {
        routing.route("/api/v3") {
            get("/pet/random") {
                call.respond(petService.getRandomPet())
            }

            get("/pet/random/{count}") {
                val count = call.parameters["count"]?.toIntOrNull() ?: 1
                call.respond(petService.getRandomPets(count))
            }
        }
    }
}