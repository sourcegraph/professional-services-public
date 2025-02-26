package com.sourcegraph.demo.app.service

import com.sourcegraph.demo.openapi.generated.model.Category
import com.sourcegraph.demo.openapi.generated.model.Pet
import com.sourcegraph.demo.openapi.generated.model.Tag
import kotlin.random.Random

class PetService {
    private val petNames = listOf("Max", "Bella", "Luna", "Charlie", "Lucy", "Cooper", "Daisy", "Milo", "Zoe", "Rocky")
    private val categories = listOf(
        Category(id = 1, name = "Dogs"),
        Category(id = 2, name = "Cats"),
        Category(id = 3, name = "Birds"),
        Category(id = 4, name = "Fish"),
        Category(id = 5, name = "Reptiles")
    )

    fun getRandomPet(): Pet {
        val name = petNames.random()
        val photoUrls = listOf("https://example.com/pet/${Random.nextLong(1, 1000)}.jpg")
        val id = Random.nextLong(1, 1000)
        val category = categories.random()
        val tags = listOf(
            Tag(id = Random.nextLong(1, 100), name = "cute"),
            Tag(id = Random.nextLong(1, 100), name = "friendly")
        )
        val status = when (Random.nextInt(3)) {
            0 -> Pet.Status.AVAILABLE
            1 -> Pet.Status.PENDING
            else -> Pet.Status.SOLD
        }

        // Create Pet object according to the constructor's parameter order from generated class
        return Pet(
            name = name,
            photoUrls = photoUrls,
            id = id,
            category = category,
            tags = tags,
            status = status
        )
    }

    fun getRandomPets(count: Int): List<Pet> {
        return (1..count).map { getRandomPet() }
    }
}